"""Authorization consistency: every id-taking job/run route hides another
non-admin's resource as 404, and every list/aggregate route is owner-scoped.

``test_every_id_route_is_covered`` walks the live route table, so a new
``{job_id}``/``{run_id}`` route fails the suite until it is added to ``ID_ROUTES``
(and therefore proven to 404 for a non-owner).
"""
import json
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session

from src.auth import hash_password
from src.models import Job, Run, User

# (method, path template) — every route keyed by a job or run id.
ID_ROUTES = [
    ("GET", "/api/jobs/{job_id}"),
    ("PATCH", "/api/jobs/{job_id}"),
    ("DELETE", "/api/jobs/{job_id}"),
    ("POST", "/api/jobs/{job_id}/run"),
    ("GET", "/api/jobs/{job_id}/overview"),
    ("GET", "/api/runs/{run_id}"),
    ("DELETE", "/api/runs/{run_id}"),
    ("POST", "/api/runs/{run_id}/cancel"),
    ("GET", "/api/runs/{run_id}/steps"),
    ("GET", "/api/runs/{run_id}/stream"),
    ("GET", "/api/runs/{run_id}/report.pdf"),
    ("GET", "/api/runs/{run_id}/leads.csv"),
]


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


def _job(engine, owner, *, job_type="email_collect", schedule=None, name="j"):
    with Session(engine) as s:
        j = Job(name=name, job_type=job_type, payload=json.dumps({"secret": name}),
                schedule=schedule, created_by=owner)
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _run(engine, job_id, user_id, status="success"):
    with Session(engine) as s:
        r = Run(job_id=job_id, user_id=user_id, status=status,
                result=json.dumps({"leads": []}), llm_provider="openai",
                llm_model="m", cost_usd=1.0)
        s.add(r)
        s.commit()
        s.refresh(r)
        return r.id


async def _login(client, name):
    resp = await client.post("/api/auth/login", json={"username": name, "password": "password123"})
    assert resp.status_code == 200, resp.text


@pytest.fixture
def reports_root(tmp_path, mocker):
    mocker.patch("src.routers.runs.REPORTS_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def bobs(test_engine, reports_root, mocker):
    """alice + bob (non-admins) + boss (admin); a job and a finished run owned by bob."""
    mocker.patch("src.automation.launcher._run_in_background", new=AsyncMock())
    _user(test_engine, "alice")
    bob = _user(test_engine, "bob")
    _user(test_engine, "boss", admin=True)
    job_id = _job(test_engine, bob)
    run_id = _run(test_engine, job_id, bob)
    (reports_root / f"{run_id}.pdf").write_bytes(b"%PDF-1.4 bob's financials")
    return {"job_id": job_id, "run_id": run_id}


# ── Consistency: every {id} route is 404 for another non-admin ───────────────

def test_every_id_route_is_covered():
    from src.main import app

    live = {
        (method, route.path)
        for route in app.routes
        if "{job_id}" in getattr(route, "path", "") or "{run_id}" in getattr(route, "path", "")
        for method in route.methods - {"HEAD", "OPTIONS"}
    }
    assert live == set(ID_ROUTES), (
        "id-taking routes changed — add the new route to ID_ROUTES so it is "
        f"checked for owner scoping: {live ^ set(ID_ROUTES)}"
    )


@pytest.mark.parametrize("method,template", ID_ROUTES)
async def test_id_route_hidden_from_other_user(anon_client, test_engine, bobs, method, template):
    await _login(anon_client, "alice")
    kwargs = {"json": {"name": "hijacked"}} if method == "PATCH" else {}
    resp = await anon_client.request(method, template.format(**bobs), **kwargs)
    assert resp.status_code == 404, f"{method} {template} → {resp.status_code}"

    # …and nothing was mutated or launched on bob's behalf.
    with Session(test_engine) as s:
        job = s.get(Job, bobs["job_id"])
        assert job is not None and job.name == "j"
        assert s.get(Run, bobs["run_id"]) is not None
        assert len(s.exec(Run.__table__.select()).all()) == 1


@pytest.mark.parametrize("who", ["bob", "boss"])
@pytest.mark.parametrize("method,template", [r for r in ID_ROUTES if r[0] == "GET"])
async def test_id_route_visible_to_owner_and_admin(anon_client, bobs, who, method, template):
    await _login(anon_client, who)
    if template.endswith("/stream"):
        async with anon_client.stream(method, template.format(**bobs)) as resp:
            assert resp.status_code == 200
        return
    resp = await anon_client.request(method, template.format(**bobs))
    assert resp.status_code == 200, f"{method} {template} → {resp.status_code}"


async def test_ownerless_legacy_job_is_admin_only(anon_client, test_engine):
    _user(test_engine, "alice")
    _user(test_engine, "boss", admin=True)
    job_id = _job(test_engine, None)

    await _login(anon_client, "alice")
    assert (await anon_client.get(f"/api/jobs/{job_id}")).status_code == 404
    assert (await anon_client.get("/api/jobs")).json() == []

    await anon_client.post("/api/auth/logout")
    await _login(anon_client, "boss")
    assert (await anon_client.get(f"/api/jobs/{job_id}")).status_code == 200


# ── Lists and aggregates are owner-scoped ────────────────────────────────────

async def test_job_list_is_scoped_per_user(anon_client, test_engine):
    alice = _user(test_engine, "alice")
    bob = _user(test_engine, "bob")
    _user(test_engine, "boss", admin=True)
    ja = _job(test_engine, alice, name="alice-job")
    jb = _job(test_engine, bob, name="bob-job")

    await _login(anon_client, "alice")
    assert {j["id"] for j in (await anon_client.get("/api/jobs")).json()} == {ja}

    await anon_client.post("/api/auth/logout")
    await _login(anon_client, "bob")
    assert {j["id"] for j in (await anon_client.get("/api/jobs")).json()} == {jb}

    await anon_client.post("/api/auth/logout")
    await _login(anon_client, "boss")
    assert {j["id"] for j in (await anon_client.get("/api/jobs")).json()} == {ja, jb}


async def test_schedules_are_scoped_per_user(anon_client, test_engine):
    alice = _user(test_engine, "alice")
    bob = _user(test_engine, "bob")
    _user(test_engine, "boss", admin=True)
    ja = _job(test_engine, alice, schedule="0 8 * * *")
    jb = _job(test_engine, bob, schedule="0 9 * * *")

    await _login(anon_client, "alice")
    assert {s["job_id"] for s in (await anon_client.get("/api/schedules")).json()} == {ja}

    await anon_client.post("/api/auth/logout")
    await _login(anon_client, "boss")
    assert {s["job_id"] for s in (await anon_client.get("/api/schedules")).json()} == {ja, jb}


async def test_overview_is_scoped_per_user(anon_client, test_engine):
    alice = _user(test_engine, "alice")
    bob = _user(test_engine, "bob")
    _user(test_engine, "boss", admin=True)
    ja = _job(test_engine, alice, job_type="email_collect")
    jb = _job(test_engine, bob, job_type="web_scraper")
    jb2 = _job(test_engine, bob, job_type="email_collect")
    ra = _run(test_engine, ja, alice)
    _run(test_engine, jb, bob)
    rb2 = _run(test_engine, jb2, bob)

    await _login(anon_client, "alice")
    index = (await anon_client.get("/api/overview")).json()
    assert index == [{"job_type": "email_collect", "run_count": 1,
                      "last_run_at": index[0]["last_run_at"]}]
    grid = (await anon_client.get("/api/overview/email_collect")).json()
    assert [r["run_id"] for r in grid["runs"]] == [ra]
    assert (await anon_client.get("/api/overview/web_scraper")).json()["runs"] == []

    await anon_client.post("/api/auth/logout")
    await _login(anon_client, "boss")
    assert {r["job_type"] for r in (await anon_client.get("/api/overview")).json()} == {
        "email_collect", "web_scraper"}
    grid = (await anon_client.get("/api/overview/email_collect")).json()
    assert {r["run_id"] for r in grid["runs"]} == {ra, rb2}


async def test_stats_are_scoped_per_user(anon_client, test_engine):
    alice = _user(test_engine, "alice")
    bob = _user(test_engine, "bob")
    _user(test_engine, "carol")
    _user(test_engine, "boss", admin=True)
    ja = _job(test_engine, alice, job_type="email_collect")
    jb = _job(test_engine, bob, job_type="web_scraper")
    _run(test_engine, ja, alice)
    _run(test_engine, jb, bob)
    _run(test_engine, jb, bob, status="failed")

    await _login(anon_client, "alice")
    stats = (await anon_client.get("/api/stats")).json()
    assert stats["total_runs"] == 1
    assert stats["success"] == 1 and stats["failed"] == 0
    assert stats["total_cost_usd"] == 1.0
    assert set(stats["by_type"]) == {"email_collect"}
    assert stats["by_provider"]["openai"]["runs"] == 1
    assert stats["by_model"]["openai"][0]["runs"] == 1
    assert sum(d["total"] for d in stats["trend"]) == 1

    # A user with no runs gets the empty shape, not everyone else's totals.
    await anon_client.post("/api/auth/logout")
    await _login(anon_client, "carol")
    assert (await anon_client.get("/api/stats")).json()["total_runs"] == 0

    await anon_client.post("/api/auth/logout")
    await _login(anon_client, "boss")
    stats = (await anon_client.get("/api/stats")).json()
    assert stats["total_runs"] == 3
    assert set(stats["by_type"]) == {"email_collect", "web_scraper"}
    assert stats["total_cost_usd"] == 3.0
