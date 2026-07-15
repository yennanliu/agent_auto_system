"""Integration tests for the /api/sessions endpoints."""
import time

import pytest

from src.automation import browser_session as bs


@pytest.fixture(autouse=True)
def _clear_tasks():
    with bs._LOCK:
        bs._TASKS.clear()
    yield
    with bs._LOCK:
        bs._TASKS.clear()


async def test_list_sessions_requires_auth(anon_client):
    resp = await anon_client.get("/api/sessions")
    assert resp.status_code == 401


async def test_list_sessions(client):
    resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert "login_enabled" in data
    names = {s["name"] for s in data["sessions"]}
    assert {"tasker", "tw104", "shopee"} <= names


async def test_get_session_shape(client):
    data = (await client.get("/api/sessions/tasker")).json()
    assert data["name"] == "tasker"
    assert "fresh" in data and "exists" in data and "state_path" in data


async def test_get_session_unknown_404(client):
    resp = await client.get("/api/sessions/nope")
    assert resp.status_code == 404


async def test_refresh_unknown_404(client):
    resp = await client.post("/api/sessions/nope/login")
    assert resp.status_code == 404


async def test_refresh_disabled_403(client, monkeypatch):
    monkeypatch.setenv("BROWSER_LOGIN_ENABLED", "0")
    resp = await client.post("/api/sessions/tasker/login")
    assert resp.status_code == 403


async def test_refresh_starts_login(client, monkeypatch):
    monkeypatch.setenv("BROWSER_LOGIN_ENABLED", "1")

    # Swap the real browser runner for a fast stub so no Chromium is launched.
    def runner(spec, on_progress, timeout):
        on_progress("working")
        return {"ok": True, "message": "saved", "state_path": "/x.json"}

    monkeypatch.setattr(bs, "_browser_login", runner)

    resp = await client.post("/api/sessions/tasker/login")
    assert resp.status_code == 202
    assert resp.json()["status"] == "running"

    # Poll the status endpoint until the background task completes.
    deadline = time.time() + 3
    status = None
    while time.time() < deadline:
        status = (await client.get("/api/sessions/tasker")).json()["last_login"]
        if status and status["status"] == "succeeded":
            break
        time.sleep(0.05)
    assert status and status["status"] == "succeeded"
    assert status["message"] == "saved"


async def test_refresh_conflict_when_running(client, monkeypatch):
    monkeypatch.setenv("BROWSER_LOGIN_ENABLED", "1")
    release = {"go": False}

    def slow(spec, on_progress, timeout):
        while not release["go"]:
            time.sleep(0.01)
        return {"ok": True, "message": "done"}

    monkeypatch.setattr(bs, "_browser_login", slow)
    first = await client.post("/api/sessions/tasker/login")
    assert first.status_code == 202
    try:
        second = await client.post("/api/sessions/tasker/login")
        assert second.status_code == 409
    finally:
        release["go"] = True
