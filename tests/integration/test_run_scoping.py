"""Activity scoping: users see only their own runs; admins see everyone's."""
import json
from unittest.mock import AsyncMock

from sqlmodel import Session

from src.auth import hash_password
from src.models import Job, Run, User


def _user(engine, name, *, admin=False):
    with Session(engine) as s:
        u = User(
            username=name,
            password_hash=hash_password("password123"),
            is_admin=admin,
            is_active=True,
            allowed_automations="*",
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _job(engine):
    with Session(engine) as s:
        j = Job(name="j", job_type="web_scraper", payload=json.dumps({"url": "https://x"}))
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _run(engine, job_id, user_id, status="success"):
    with Session(engine) as s:
        r = Run(job_id=job_id, user_id=user_id, status=status, result=json.dumps({"ok": True}))
        s.add(r)
        s.commit()
        s.refresh(r)
        return r.id


async def _login(client, name):
    resp = await client.post("/api/auth/login", json={"username": name, "password": "password123"})
    assert resp.status_code == 200, resp.text


# ── Visibility ────────────────────────────────────────────────────────────────

async def test_user_sees_only_their_runs(anon_client, test_engine):
    alice = _user(test_engine, "alice")
    bob = _user(test_engine, "bob")
    job = _job(test_engine)
    ra = _run(test_engine, job, alice)
    rb = _run(test_engine, job, bob)

    await _login(anon_client, "alice")
    ids = {r["id"] for r in (await anon_client.get("/api/runs")).json()}
    assert ra in ids
    assert rb not in ids


async def test_admin_sees_all_runs_with_owner(anon_client, test_engine):
    _user(test_engine, "boss", admin=True)
    alice = _user(test_engine, "alice")
    job = _job(test_engine)
    ra = _run(test_engine, job, alice)

    await _login(anon_client, "boss")
    runs = (await anon_client.get("/api/runs")).json()
    row = next(r for r in runs if r["id"] == ra)
    assert row["owner"] == "alice"


# ── Per-run access control ────────────────────────────────────────────────────

async def test_user_cannot_access_others_run(anon_client, test_engine):
    _user(test_engine, "alice")
    bob = _user(test_engine, "bob")
    job = _job(test_engine)
    rb = _run(test_engine, job, bob)

    await _login(anon_client, "alice")
    assert (await anon_client.get(f"/api/runs/{rb}")).status_code == 404
    assert (await anon_client.delete(f"/api/runs/{rb}")).status_code == 404
    assert (await anon_client.post(f"/api/runs/{rb}/cancel")).status_code == 404


async def test_admin_can_access_any_run(anon_client, test_engine):
    _user(test_engine, "boss", admin=True)
    alice = _user(test_engine, "alice")
    job = _job(test_engine)
    ra = _run(test_engine, job, alice)

    await _login(anon_client, "boss")
    assert (await anon_client.get(f"/api/runs/{ra}")).status_code == 200


# ── Bulk delete is scoped too ─────────────────────────────────────────────────

async def test_bulk_delete_only_removes_own(anon_client, test_engine):
    alice = _user(test_engine, "alice")
    bob = _user(test_engine, "bob")
    job = _job(test_engine)
    _run(test_engine, job, alice)
    rb = _run(test_engine, job, bob)

    await _login(anon_client, "alice")
    assert (await anon_client.delete("/api/runs?delete_all=true")).json()["deleted"] == 1

    await anon_client.post("/api/auth/logout")
    await _login(anon_client, "bob")
    assert {r["id"] for r in (await anon_client.get("/api/runs")).json()} == {rb}


# ── Triggering records the owner ──────────────────────────────────────────────

async def test_trigger_records_owner(anon_client, test_engine, mocker):
    mocker.patch("src.automation.launcher._run_in_background", new=AsyncMock())
    alice = _user(test_engine, "alice")
    job = _job(test_engine)

    await _login(anon_client, "alice")
    resp = await anon_client.post(f"/api/jobs/{job}/run")
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    with Session(test_engine) as s:
        assert s.get(Run, run_id).user_id == alice
