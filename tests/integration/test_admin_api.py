"""Integration tests for the admin API and per-automation run enforcement."""
import json

import pytest_asyncio
from sqlmodel import Session

from src.auth import hash_password
from src.models import Job, User


def _make_user(test_engine, username, *, is_admin=False, allowed="[]", active=True):
    with Session(test_engine) as s:
        u = User(
            username=username,
            password_hash=hash_password("password123"),
            is_admin=is_admin,
            is_active=active,
            allowed_automations=allowed,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


@pytest_asyncio.fixture
async def user_client(anon_client, test_engine):
    """A logged-in NON-admin allowed to run only web_scraper."""
    _make_user(test_engine, "bob", allowed=json.dumps(["web_scraper"]))
    resp = await anon_client.post(
        "/api/auth/login", json={"username": "bob", "password": "password123"}
    )
    assert resp.status_code == 200
    yield anon_client


# ── Admin gating ──────────────────────────────────────────────────────────────

async def test_admin_routes_require_admin(user_client):
    assert (await user_client.get("/api/admin/users")).status_code == 403


async def test_admin_routes_require_auth(anon_client):
    assert (await anon_client.get("/api/admin/users")).status_code == 401


# ── User management ───────────────────────────────────────────────────────────

async def test_create_and_list_users(client):
    resp = await client.post(
        "/api/admin/users",
        json={"username": "carol", "password": "password123", "allowed_automations": ["web_scraper"]},
    )
    assert resp.status_code == 201
    assert resp.json()["allowed_automations"] == ["web_scraper"]

    users = (await client.get("/api/admin/users")).json()
    assert {u["username"] for u in users} == {"tester", "carol"}


async def test_create_user_duplicate(client):
    await client.post("/api/admin/users", json={"username": "dup", "password": "password123"})
    resp = await client.post("/api/admin/users", json={"username": "dup", "password": "password123"})
    assert resp.status_code == 409


async def test_create_user_short_password(client):
    resp = await client.post("/api/admin/users", json={"username": "x", "password": "short"})
    assert resp.status_code == 400


async def test_wildcard_allowlist(client):
    resp = await client.post(
        "/api/admin/users",
        json={"username": "power", "password": "password123", "allowed_automations": ["*"]},
    )
    assert resp.json()["allowed_automations"] == "*"


async def test_update_user_allowlist_and_active(client, test_engine):
    uid = _make_user(test_engine, "dave")
    resp = await client.patch(
        f"/api/admin/users/{uid}",
        json={"is_active": False, "allowed_automations": ["email_sender"]},
    )
    body = resp.json()
    assert body["is_active"] is False
    assert body["allowed_automations"] == ["email_sender"]


async def test_reset_password(client, test_engine):
    uid = _make_user(test_engine, "erin")
    resp = await client.post(f"/api/admin/users/{uid}/password", json={"new_password": "brandnew123"})
    assert resp.status_code == 200


async def test_delete_user(client, test_engine):
    uid = _make_user(test_engine, "frank")
    assert (await client.delete(f"/api/admin/users/{uid}")).status_code == 204
    users = (await client.get("/api/admin/users")).json()
    assert "frank" not in {u["username"] for u in users}


# ── Last-admin guardrails ─────────────────────────────────────────────────────

async def test_cannot_demote_last_admin(client):
    # 'tester' (the seeded admin) is the only admin.
    me = (await client.get("/api/auth/me")).json()
    resp = await client.patch(f"/api/admin/users/{me['id']}", json={"is_admin": False})
    assert resp.status_code == 400


async def test_cannot_delete_own_account(client):
    me = (await client.get("/api/auth/me")).json()
    resp = await client.delete(f"/api/admin/users/{me['id']}")
    assert resp.status_code == 400


# ── LLM keys ──────────────────────────────────────────────────────────────────

async def test_set_and_list_llm_key(client):
    resp = await client.put("/api/admin/llm-keys/openai", json={"api_key": "sk-test-key-123456"})
    assert resp.status_code == 200
    assert resp.json()["source"] == "db"

    keys = (await client.get("/api/admin/llm-keys")).json()
    openai = next(k for k in keys if k["provider"] == "openai")
    assert openai["configured"] is True
    assert "sk-test-key-123456" not in json.dumps(keys)  # plaintext never leaks


async def test_set_llm_key_unknown_provider(client):
    assert (await client.put("/api/admin/llm-keys/bogus", json={"api_key": "x"})).status_code == 404


# ── Automation toggles ────────────────────────────────────────────────────────

async def test_get_and_set_automations(client):
    resp = await client.put("/api/admin/automations", json={"enabled": ["web_scraper"]})
    assert resp.json()["enabled"] == ["web_scraper"]
    assert (await client.get("/api/admin/automations")).json()["enabled"] == ["web_scraper"]


async def test_eval_judge_defaults_to_auto(client):
    data = (await client.get("/api/admin/eval-judge")).json()
    assert data["provider"] is None  # Auto
    assert data["default"]["provider"] == "gemini"
    assert "gemini" in data["providers"]


async def test_set_and_get_eval_judge(client):
    resp = await client.put("/api/admin/eval-judge",
                            json={"provider": "gemini", "model": "gemini/gemini-2.5-flash"})
    assert resp.status_code == 200
    assert resp.json()["provider"] == "gemini"
    data = (await client.get("/api/admin/eval-judge")).json()
    assert data["model"] == "gemini/gemini-2.5-flash"


async def test_set_eval_judge_clears_on_empty(client):
    await client.put("/api/admin/eval-judge", json={"provider": "gemini", "model": None})
    resp = await client.put("/api/admin/eval-judge", json={"provider": None})
    assert resp.json()["provider"] is None  # reverted to Auto


async def test_set_eval_judge_unknown_provider(client):
    assert (await client.put("/api/admin/eval-judge",
                             json={"provider": "bogus"})).status_code == 404


async def test_set_eval_judge_unknown_model(client):
    assert (await client.put("/api/admin/eval-judge",
                             json={"provider": "gemini", "model": "gemini/nope"})).status_code == 400


# ── Per-automation run enforcement ────────────────────────────────────────────

async def test_user_can_create_allowed_job(user_client):
    resp = await user_client.post(
        "/api/jobs",
        json={"name": "scrape", "job_type": "web_scraper", "payload": {"url": "https://example.com"}},
    )
    assert resp.status_code == 201


async def test_user_blocked_from_disallowed_job(user_client):
    resp = await user_client.post(
        "/api/jobs",
        json={"name": "mail", "job_type": "email_sender", "payload": {}},
    )
    assert resp.status_code == 403


async def test_disabled_automation_blocks_everyone(client):
    # Disable web_scraper globally; even the admin is blocked from creating it.
    await client.put("/api/admin/automations", json={"enabled": ["email_sender"]})
    resp = await client.post(
        "/api/jobs",
        json={"name": "scrape", "job_type": "web_scraper", "payload": {"url": "https://example.com"}},
    )
    assert resp.status_code == 403


async def test_run_blocked_when_not_allowed(user_client, test_engine):
    # A job 'bob' may not run (email_sender) still can't be triggered by him.
    with Session(test_engine) as s:
        job = Job(name="mail", job_type="email_sender", payload=json.dumps({}))
        s.add(job)
        s.commit()
        s.refresh(job)
        job_id = job.id
    resp = await user_client.post(f"/api/jobs/{job_id}/run")
    assert resp.status_code == 403


# ── Scraper drift canaries (E7) ───────────────────────────────────────────────

async def test_canaries_endpoint_is_admin_only(user_client):
    assert (await user_client.get("/api/admin/canaries")).status_code == 403


async def test_canaries_endpoint_lists_latest_status(client, test_engine):
    from src.automation import canary

    with Session(test_engine) as s:
        canary.record(s, "tw104_apply", canary.Outcome(canary.OK, "fine"))
        canary.record(s, "tw104_apply", canary.Outcome(canary.FAILED, "cards gone"))

    resp = await client.get("/api/admin/canaries")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False  # nightly trigger is opt-in
    by_type = {a["job_type"]: a for a in body["automations"]}
    tw = by_type["tw104_apply"]
    assert tw["latest"]["status"] == "failed" and tw["latest"]["alert"] == "failing"
    assert tw["alerting"] is True and tw["last_ok_at"]
    assert by_type["linkedin_apply"]["canary"]["session"] == "linkedin"
    assert by_type["linkedin_apply"]["latest"] is None
    assert by_type["x_scraper"]["canary"] is None and by_type["x_scraper"]["skip_reason"]
    assert "web_scraper" not in by_type  # not a browser automation
