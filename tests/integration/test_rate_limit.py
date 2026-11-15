"""Rate limiting on login (per IP / per username+IP) and run trigger (per user).

The suite-wide autouse fixture turns limiting off; these tests turn it back on.
"""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from src import rate_limit


@pytest.fixture
def limits_on(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_IP", "3/minute")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_USERNAME", "100/minute")
    monkeypatch.setenv("RATE_LIMIT_RUN", "2/minute")


def _client(ip: str) -> AsyncClient:
    from src.main import app
    return AsyncClient(
        transport=ASGITransport(app=app, client=(ip, 5000)), base_url="http://test"
    )


async def _login(c, username="tester", password="wrong", **kw):
    return await c.post(
        "/api/auth/login", json={"username": username, "password": password}, **kw
    )


async def test_login_429_after_limit_with_retry_after(anon_client, seed_admin, limits_on):
    for _ in range(3):
        assert (await _login(anon_client)).status_code == 401
    resp = await _login(anon_client)
    assert resp.status_code == 429
    assert 1 <= int(resp.headers["Retry-After"]) <= 60
    assert "Too many" in resp.json()["detail"]
    # Locked out means locked out: the right password gets no answer either.
    assert (await _login(anon_client, password="password123")).status_code == 429


async def test_login_other_ip_unaffected(anon_client, seed_admin, limits_on):
    async with _client("10.0.0.1") as a, _client("10.0.0.2") as b:
        for _ in range(3):
            await _login(a)
        assert (await _login(a)).status_code == 429
        assert (await _login(b)).status_code == 401
        assert (await _login(b, password="password123")).status_code == 200


async def test_login_ignores_spoofed_forwarded_for(anon_client, seed_admin, limits_on):
    for i in range(3):
        await _login(anon_client, headers={"X-Forwarded-For": f"1.2.3.{i}"})
    resp = await _login(anon_client, headers={"X-Forwarded-For": "9.9.9.9"})
    assert resp.status_code == 429


async def test_successful_logins_are_not_counted(anon_client, seed_admin, limits_on):
    for _ in range(6):
        assert (await _login(anon_client, password="password123")).status_code == 200


async def test_login_username_limit_per_ip(anon_client, seed_admin, limits_on, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_LOGIN_IP", "100/minute")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_USERNAME", "2/minute")
    async with _client("10.0.0.1") as a:
        assert (await _login(a)).status_code == 401
        assert (await _login(a, username="TESTER ")).status_code == 401  # same account
        assert (await _login(a)).status_code == 429
        assert (await _login(a, username="someone-else")).status_code == 401


async def test_attacker_cannot_lock_out_real_user(anon_client, seed_admin, limits_on, monkeypatch):
    """Failing on purpose for a known username only burns the attacker's own budget."""
    monkeypatch.setenv("RATE_LIMIT_LOGIN_IP", "100/minute")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_USERNAME", "2/minute")
    async with _client("6.6.6.6") as attacker, _client("10.0.0.7") as victim:
        for _ in range(5):
            await _login(attacker, username="tester")
        assert (await _login(attacker, username="tester")).status_code == 429
        assert (await _login(victim, password="password123")).status_code == 200


async def test_success_clears_account_counter(anon_client, seed_admin, limits_on, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_LOGIN_IP", "100/minute")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_USERNAME", "2/minute")
    assert (await _login(anon_client)).status_code == 401
    assert (await _login(anon_client, password="password123")).status_code == 200
    # Budget is back to full: two more typos before 429.
    assert (await _login(anon_client)).status_code == 401
    assert (await _login(anon_client)).status_code == 401
    assert (await _login(anon_client)).status_code == 429


async def test_success_does_not_refill_ip_budget(anon_client, seed_admin, limits_on):
    """Logging into your own account mustn't reset the budget for guessing others."""
    for _ in range(2):
        assert (await _login(anon_client, username="victim")).status_code == 401
    assert (await _login(anon_client, password="password123")).status_code == 200
    assert (await _login(anon_client, username="victim")).status_code == 401
    assert (await _login(anon_client, username="victim")).status_code == 429


async def test_concurrent_logins_cannot_overshoot(anon_client, seed_admin, limits_on, mocker):
    """Slots are reserved before bcrypt: a burst of N gets exactly the budget, no more."""
    def slow_verify(*_a):
        time.sleep(0.2)
        return False

    mocker.patch("src.routers.auth.verify_password", side_effect=slow_verify)
    resps = await asyncio.gather(*(_login(anon_client) for _ in range(8)))
    codes = sorted(r.status_code for r in resps)
    assert codes == [401] * 3 + [429] * 5


def test_prune_drops_expired_keys(monkeypatch):
    monkeypatch.setattr(rate_limit, "_PRUNE_AT", 3)
    events = rate_limit._storage.events
    for i in range(5):
        events[f"stale-{i}"] = []
    events["live"] = ["entry"]
    rate_limit._prune()
    assert not any(k.startswith("stale-") for k in events)
    assert "live" in events


async def test_login_limit_disabled_switch(anon_client, seed_admin, limits_on, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "0")
    for _ in range(8):
        assert (await _login(anon_client)).status_code == 401


async def test_empty_value_disables_one_limit(anon_client, seed_admin, limits_on, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_LOGIN_IP", "")
    for _ in range(8):
        assert (await _login(anon_client)).status_code == 401


def test_invalid_value_falls_back_to_default(limits_on, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_RUN", "lots please")
    for _ in range(20):
        rate_limit.hit_run_trigger(1)
    with pytest.raises(Exception) as exc:
        rate_limit.hit_run_trigger(1)
    assert exc.value.status_code == 429


async def test_run_trigger_limited_per_user(client, seed_job, test_engine, limits_on, mocker):
    from sqlmodel import Session

    from src.auth import hash_password
    from src.models import User

    mocker.patch("src.automation.launcher._run_in_background", new=AsyncMock())
    with Session(test_engine) as s:
        s.add(User(username="other", password_hash=hash_password("password123"),
                   is_admin=True, is_active=True, allowed_automations="*"))
        s.commit()

    url = f"/api/jobs/{seed_job.id}/run"
    assert (await client.post(url)).status_code == 202
    assert (await client.post(url)).status_code == 202
    resp = await client.post(url)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    assert "Too many" in resp.json()["detail"]

    # Same client IP, different user → own budget (keyed by user id, not IP).
    async with _client("127.0.0.1") as other:
        assert (await _login(other, username="other", password="password123")).status_code == 200
        assert (await other.post(url)).status_code == 202


async def test_run_trigger_disabled_switch(client, seed_job, limits_on, monkeypatch, mocker):
    mocker.patch("src.automation.launcher._run_in_background", new=AsyncMock())
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "0")
    for _ in range(5):
        assert (await client.post(f"/api/jobs/{seed_job.id}/run")).status_code == 202


async def test_rejected_run_is_not_created(client, seed_job, limits_on, mocker):
    launched = mocker.patch("src.routers.runs.launch_run", return_value=1)
    for _ in range(4):
        await client.post(f"/api/jobs/{seed_job.id}/run")
    assert launched.call_count == 2


async def test_scheduler_launches_are_not_rate_limited(test_engine, limits_on, mocker):
    """The scheduler calls launch_run directly — far past the per-user budget is fine."""
    from sqlmodel import Session, select

    from src.automation.launcher import launch_run
    from src.models import Job, Run

    mocker.patch("src.automation.launcher.get_engine", return_value=test_engine)
    mocker.patch("src.automation.launcher._run_in_background", new=AsyncMock())
    with Session(test_engine) as s:
        job = Job(name="Cron", job_type="google_form_fill", payload="{}", schedule="* * * * *")
        s.add(job)
        s.commit()
        job_id = job.id

    for _ in range(10):
        launch_run(job_id, "google_form_fill", {}, user_id=1, trigger="schedule")
    with Session(test_engine) as s:
        assert len(s.exec(select(Run)).all()) == 10
