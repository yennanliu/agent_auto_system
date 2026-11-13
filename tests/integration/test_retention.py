"""Data retention purge + admin export/erase of a user's data (E4)."""
import io
import json
import os
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlmodel import Session, select

import src.automation.report_render as reports_mod
import src.database as db_mod
import src.routers.uploads as uploads_mod
from src import retention
from src.auth import hash_password
from src.models import Job, Run, User

NOW = datetime(2026, 6, 1, tzinfo=UTC)
UP_A = "a" * 32
UP_B = "b" * 32
UP_C = "c" * 32


@pytest.fixture(autouse=True)
def _env(test_engine, tmp_path, mocker, monkeypatch):
    """Temp DB + temp uploads/reports dirs + a clean retention env."""
    mocker.patch.object(db_mod, "engine", test_engine)
    mocker.patch.object(uploads_mod, "UPLOAD_ROOT", tmp_path / "uploads")
    mocker.patch.object(reports_mod, "REPORTS_ROOT", tmp_path / "reports")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "reports").mkdir()
    for var in ("RETENTION_ENABLED", "RETENTION_RUN_DAYS", "RETENTION_UPLOAD_DAYS",
                "RETENTION_INTERVAL_HOURS"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _user(engine, username, *, is_admin=False) -> int:
    with Session(engine) as s:
        u = User(username=username, password_hash=hash_password("password123"),
                 is_admin=is_admin, is_active=True, allowed_automations="*")
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _job(engine, *, owner=None, payload=None, job_type="web_scraper") -> int:
    with Session(engine) as s:
        j = Job(name="j", job_type=job_type, payload=json.dumps(payload or {}), created_by=owner)
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _run(engine, job_id, *, age_days=0, status="success", user_id=None, finished=True) -> int:
    started = NOW - timedelta(days=age_days)
    with Session(engine) as s:
        r = Run(job_id=job_id, user_id=user_id, status=status, started_at=started,
                finished_at=started if finished else None,
                result=json.dumps({"leads": [{"responsible": "王小明"}]}))
        s.add(r)
        s.commit()
        s.refresh(r)
        return r.id


def _run_ids(engine) -> set[int]:
    with Session(engine) as s:
        return set(s.exec(select(Run.id)).all())


def _upload(root, upload_id, *, age_days=0):
    d = root / "uploads" / upload_id
    d.mkdir(parents=True)
    (d / "sales.csv").write_text("a,b\n1,2\n")
    ts = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(d / "sales.csv", (ts, ts))
    os.utime(d, (ts, ts))
    return d


def _report(root, run_id, *, age_days=0):
    p = root / "reports" / f"{run_id}.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    ts = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(p, (ts, ts))
    return p


# ── Policy ────────────────────────────────────────────────────────────────────

def test_policy_defaults_are_opt_in():
    pol = retention.policy()
    assert pol.enabled is False
    assert (pol.run_days, pol.upload_days, pol.interval_hours) == (90, 30, 24)


def test_policy_reads_env_and_bad_values_disable(monkeypatch):
    monkeypatch.setenv("RETENTION_ENABLED", "1")
    monkeypatch.setenv("RETENTION_RUN_DAYS", "7")
    monkeypatch.setenv("RETENTION_UPLOAD_DAYS", "banana")
    monkeypatch.setenv("RETENTION_INTERVAL_HOURS", "-3")
    pol = retention.policy()
    assert pol.enabled is True
    assert pol.run_days == 7
    assert pol.upload_days == 0       # malformed → category disabled, never guessed
    assert pol.interval_hours == 24   # malformed → default cadence


# ── Purge ─────────────────────────────────────────────────────────────────────

def test_purge_deletes_only_expired_finished_runs(test_engine, _env):
    job = _job(test_engine)
    old = _run(test_engine, job, age_days=100)
    old_failed = _run(test_engine, job, age_days=91, status="failed")
    fresh = _run(test_engine, job, age_days=89)
    old_running = _run(test_engine, job, age_days=200, status="running", finished=False)
    old_pending = _run(test_engine, job, age_days=200, status="pending", finished=False)
    old_pdf = _report(_env, old, age_days=100)
    fresh_pdf = _report(_env, fresh, age_days=89)
    running_pdf = _report(_env, old_running, age_days=200)

    out = retention.purge_expired(now=NOW)

    assert out["runs_deleted"] == 2 and out["reports_deleted"] == 1
    assert _run_ids(test_engine) == {fresh, old_running, old_pending}
    assert old_failed not in _run_ids(test_engine)
    assert not old_pdf.exists()
    assert fresh_pdf.exists() and running_pdf.exists()
    with Session(test_engine) as s:
        assert s.get(Job, job) is not None  # jobs are never purged


def test_purge_dry_run_deletes_nothing(test_engine, _env):
    job = _job(test_engine)
    old = _run(test_engine, job, age_days=100)
    pdf = _report(_env, old, age_days=100)
    up = _upload(_env, UP_A, age_days=100)

    out = retention.purge_expired(now=NOW, dry_run=True)

    assert out["dry_run"] is True
    assert (out["runs_deleted"], out["reports_deleted"], out["uploads_deleted"]) == (1, 1, 1)
    assert _run_ids(test_engine) == {old}
    assert pdf.exists() and up.exists()


def test_purge_zero_days_disables_a_category(test_engine, _env, monkeypatch):
    monkeypatch.setenv("RETENTION_RUN_DAYS", "0")
    job = _job(test_engine)
    old = _run(test_engine, job, age_days=1000)
    up = _upload(_env, UP_A, age_days=1000)

    out = retention.purge_expired(now=NOW)

    assert out["runs_deleted"] == 0 and out["uploads_deleted"] == 1
    assert _run_ids(test_engine) == {old}
    assert not up.exists()


def test_purge_uploads_keeps_fresh_and_referenced(test_engine, _env):
    # Referenced from a nested (pipeline-style) payload → must survive forever.
    _job(test_engine, payload={"steps": [{"payload": {"upload_id": UP_A}}]})
    referenced = _upload(_env, UP_A, age_days=400)
    expired = _upload(_env, UP_B, age_days=31)
    fresh = _upload(_env, UP_C, age_days=29)
    foreign = _env / "uploads" / "not-an-upload"
    foreign.mkdir()
    os.utime(foreign, (0, 0))

    out = retention.purge_expired(now=NOW)

    assert out["uploads_deleted"] == 1 and out["uploads_kept_referenced"] == 1
    assert referenced.exists() and fresh.exists() and foreign.exists()
    assert not expired.exists()


def test_purge_removes_old_orphan_reports_only(test_engine, _env):
    job = _job(test_engine)
    live = _run(test_engine, job, age_days=1)
    live_pdf = _report(_env, live, age_days=200)       # run still exists → keep
    orphan_old = _report(_env, 9001, age_days=200)     # no run row, old → delete
    orphan_new = _report(_env, 9002, age_days=1)       # no run row, recent → keep
    other = _env / "reports" / "notes.pdf"
    other.write_bytes(b"x")
    os.utime(other, (0, 0))

    out = retention.purge_expired(now=NOW)

    assert out["reports_deleted"] == 1
    assert not orphan_old.exists()
    assert live_pdf.exists() and orphan_new.exists() and other.exists()


# ── Path safety ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "../outside", "..", ".", "", "a/b", "/etc", "A" * 32, "a" * 31, "a" * 33,
    "a" * 32 + "/..", None, 5,
])
def test_delete_upload_rejects_malformed_ids(_env, bad):
    outside = _env / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "keep.txt").write_text("keep")
    assert retention.delete_upload(bad) is False
    assert (outside / "keep.txt").exists()


def test_delete_upload_skips_symlink_escaping_root(_env):
    outside = _env / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (_env / "uploads" / UP_A).symlink_to(outside, target_is_directory=True)
    os.utime(outside, (0, 0))

    assert retention.delete_upload(UP_A) is False
    out = retention.purge_expired(now=NOW)

    assert out["uploads_deleted"] == 0
    assert (outside / "keep.txt").exists()


def test_delete_report_skips_symlink_and_bad_ids(_env):
    target = _env / "secret.pdf"
    target.write_bytes(b"secret")
    (_env / "reports" / "7.pdf").symlink_to(target)
    assert retention.delete_report(7) is False
    assert retention.delete_report(-1) is False
    assert retention.delete_report("../secret") is False  # type: ignore[arg-type]
    assert target.exists()


def test_symlink_inside_upload_is_unlinked_not_followed(_env):
    outside = _env / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    up = _upload(_env, UP_A)
    (up / "link").symlink_to(outside, target_is_directory=True)

    assert retention.delete_upload(UP_A) is True
    assert not up.exists()
    assert (outside / "keep.txt").exists()


# ── Background task ───────────────────────────────────────────────────────────

async def test_task_does_not_start_unless_enabled(monkeypatch):
    task = retention.RetentionTask()
    assert task.start() is False
    assert task._task is None

    monkeypatch.setenv("RETENTION_ENABLED", "1")
    assert task.start() is True
    assert task._task is not None
    await task.stop()
    assert task._task is None


async def test_task_run_once_purges_and_never_raises(test_engine, _env, mocker):
    job = _job(test_engine)
    with Session(test_engine) as s:  # really old relative to the wall clock
        s.add(Run(job_id=job, status="success", started_at=datetime(2020, 1, 1, tzinfo=UTC),
                  finished_at=datetime(2020, 1, 1, tzinfo=UTC)))
        s.commit()
    task = retention.RetentionTask()
    out = await task.run_once()
    assert out["runs_deleted"] == 1 and task.last_result == out

    mocker.patch("src.retention.purge_expired", side_effect=RuntimeError("boom"))
    assert await task.run_once() is None


# ── Admin API ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def user_client(anon_client, test_engine):
    _user(test_engine, "bob")
    resp = await anon_client.post(
        "/api/auth/login", json={"username": "bob", "password": "password123"}
    )
    assert resp.status_code == 200
    yield anon_client


async def test_retention_routes_require_admin(user_client, test_engine):
    uid = _user(test_engine, "carol")
    assert (await user_client.get("/api/admin/retention")).status_code == 403
    assert (await user_client.post("/api/admin/retention/purge?dry_run=false")).status_code == 403
    assert (await user_client.get(f"/api/admin/users/{uid}/export")).status_code == 403
    assert (await user_client.delete(f"/api/admin/users/{uid}/data?confirm=carol")).status_code == 403


async def test_retention_routes_require_auth(anon_client):
    assert (await anon_client.get("/api/admin/retention")).status_code == 401
    assert (await anon_client.delete("/api/admin/users/1/data?confirm=x")).status_code == 401


async def test_get_policy_and_purge_endpoint_defaults_to_dry_run(client, test_engine):
    job = _job(test_engine)
    with Session(test_engine) as s:
        s.add(Run(job_id=job, status="success", started_at=datetime(2020, 1, 1, tzinfo=UTC),
                  finished_at=datetime(2020, 1, 1, tzinfo=UTC)))
        s.commit()

    body = (await client.get("/api/admin/retention")).json()
    assert body["policy"] == {
        "enabled": False, "run_days": 90, "upload_days": 30, "interval_hours": 24,
    }

    dry = (await client.post("/api/admin/retention/purge")).json()
    assert dry["dry_run"] is True and dry["runs_deleted"] == 1
    assert len(_run_ids(test_engine)) == 1

    real = (await client.post("/api/admin/retention/purge?dry_run=false")).json()
    assert real["dry_run"] is False and real["runs_deleted"] == 1
    assert _run_ids(test_engine) == set()


async def test_export_user_data_zip(client, test_engine, _env):
    bob = _user(test_engine, "bob")
    carol = _user(test_engine, "carol")
    bob_job = _job(test_engine, owner=bob, payload={"upload_id": UP_A}, job_type="profit_health_check")
    carol_job = _job(test_engine, owner=carol, payload={"upload_id": UP_B})
    r_own = _run(test_engine, bob_job, user_id=bob)
    r_on_bobs_job = _run(test_engine, bob_job, user_id=carol)   # derives from bob's job
    r_bob_elsewhere = _run(test_engine, carol_job, user_id=bob)
    r_carol = _run(test_engine, carol_job, user_id=carol)
    _upload(_env, UP_A)
    _upload(_env, UP_B)
    _report(_env, r_own)
    _report(_env, r_carol)

    resp = await client.get(f"/api/admin/users/{bob}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert set(zf.namelist()) == {
        "data.json", f"uploads/{UP_A}/sales.csv", f"reports/{r_own}.pdf",
    }
    doc = json.loads(zf.read("data.json"))
    assert doc["user"]["username"] == "bob"
    assert "password_hash" not in doc["user"]
    assert [j["id"] for j in doc["jobs"]] == [bob_job]
    assert doc["jobs"][0]["payload"] == {"upload_id": UP_A}
    assert {r["id"] for r in doc["runs"]} == {r_own, r_on_bobs_job, r_bob_elsewhere}
    assert doc["runs"][0]["result"]["leads"][0]["responsible"] == "王小明"
    # Export is read-only.
    assert len(_run_ids(test_engine)) == 4

    assert (await client.get("/api/admin/users/99999/export")).status_code == 404


async def test_delete_user_data(client, test_engine, _env):
    bob = _user(test_engine, "bob")
    carol = _user(test_engine, "carol")
    bob_job = _job(test_engine, owner=bob, payload={"upload_id": UP_A})
    bob_shared = _job(test_engine, owner=bob, payload={"upload_id": UP_B})
    carol_job = _job(test_engine, owner=carol, payload={"upload_id": UP_B})  # shares UP_B
    legacy_job = _job(test_engine, owner=None)
    r_own = _run(test_engine, bob_job, user_id=bob)
    r_shared = _run(test_engine, bob_shared, user_id=carol)
    r_carol = _run(test_engine, carol_job, user_id=carol)
    r_legacy = _run(test_engine, legacy_job, user_id=None)
    up_a, up_b = _upload(_env, UP_A), _upload(_env, UP_B)
    pdf_own, pdf_carol = _report(_env, r_own), _report(_env, r_carol)

    # Confirmation must match the username; nothing happens otherwise.
    assert (await client.delete(f"/api/admin/users/{bob}/data")).status_code == 422
    assert (await client.delete(f"/api/admin/users/{bob}/data?confirm=nope")).status_code == 400
    assert len(_run_ids(test_engine)) == 4

    resp = await client.delete(f"/api/admin/users/{bob}/data?confirm=bob")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "user_id": bob, "jobs_deleted": 2, "runs_deleted": 2, "reports_deleted": 1,
        "uploads_deleted": 1, "uploads_kept_referenced": 1,
    }
    assert _run_ids(test_engine) == {r_carol, r_legacy}
    assert r_shared not in _run_ids(test_engine)
    with Session(test_engine) as s:
        assert set(s.exec(select(Job.id)).all()) == {carol_job, legacy_job}
        assert s.get(User, bob) is not None  # the account itself is kept
    assert not up_a.exists() and not pdf_own.exists()
    assert up_b.exists() and pdf_carol.exists()  # still used by carol's job

    assert (await client.delete("/api/admin/users/99999/data?confirm=x")).status_code == 404


async def test_delete_user_data_refuses_while_runs_active(client, test_engine):
    bob = _user(test_engine, "bob")
    job = _job(test_engine, owner=bob)
    done = _run(test_engine, job, user_id=bob)
    active = _run(test_engine, job, user_id=bob, status="running", finished=False)

    resp = await client.delete(f"/api/admin/users/{bob}/data?confirm=bob")

    assert resp.status_code == 409
    assert _run_ids(test_engine) == {done, active}
    with Session(test_engine) as s:
        assert s.get(Job, job) is not None


# ── Deleting a run removes its PDF ────────────────────────────────────────────

async def test_deleting_runs_removes_their_reports(client, test_engine, _env):
    job = _job(test_engine)
    r1, r2, r3 = (_run(test_engine, job) for _ in range(3))
    p1, p2, p3 = (_report(_env, r) for r in (r1, r2, r3))

    assert (await client.delete(f"/api/runs/{r1}")).status_code == 204
    assert not p1.exists() and p2.exists() and p3.exists()

    assert (await client.delete(f"/api/runs?ids={r2}")).json() == {"deleted": 1}
    assert not p2.exists() and p3.exists()
