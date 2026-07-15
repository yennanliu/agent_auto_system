"""Unit tests for src.automation.browser_session — spec registry, freshness,
and the injectable login-task lifecycle (no real browser)."""
import time

import pytest

from src.automation import browser_session as bs


@pytest.fixture(autouse=True)
def _clear_tasks():
    """Each test starts with an empty in-memory task registry."""
    with bs._LOCK:
        bs._TASKS.clear()
    yield
    with bs._LOCK:
        bs._TASKS.clear()


def _wait_until(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── registry ──────────────────────────────────────────────────────────────────

def test_known_specs_present():
    names = {s.name for s in bs.all_specs()}
    assert {"tasker", "tw104", "shopee"} <= names


def test_get_spec_unknown_is_none():
    assert bs.get_spec("nope") is None


def test_state_path_prefers_env(monkeypatch):
    spec = bs.get_spec("tasker")
    monkeypatch.setenv(spec.state_env, "/custom/tasker.json")
    assert bs.state_path(spec) == "/custom/tasker.json"


def test_state_path_falls_back_to_default(monkeypatch):
    spec = bs.get_spec("tasker")
    monkeypatch.delenv(spec.state_env, raising=False)
    assert bs.state_path(spec) == spec.default_state_path


# ── status / freshness ────────────────────────────────────────────────────────

def test_status_unknown_is_none():
    assert bs.session_status("nope") is None


def test_status_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("TASKER_STORAGE_STATE", str(tmp_path / "absent.json"))
    st = bs.session_status("tasker")
    assert st["exists"] is False
    assert st["fresh"] is False
    assert st["age_seconds"] is None


def test_status_fresh_file(monkeypatch, tmp_path):
    f = tmp_path / "tasker_state.json"
    f.write_text("{}")
    monkeypatch.setenv("TASKER_STORAGE_STATE", str(f))
    st = bs.session_status("tasker")
    assert st["exists"] is True
    assert st["fresh"] is True
    assert st["age_seconds"] >= 0


def test_status_stale_file(monkeypatch, tmp_path):
    f = tmp_path / "tasker_state.json"
    f.write_text("{}")
    monkeypatch.setenv("TASKER_STORAGE_STATE", str(f))
    # Freeze "now" far in the future so the file reads as older than the TTL.
    spec = bs.get_spec("tasker")
    monkeypatch.setattr(bs, "_now", lambda: time.time() + spec.ttl_seconds + 10)
    st = bs.session_status("tasker")
    assert st["exists"] is True
    assert st["fresh"] is False
    assert st["age_seconds"] > spec.ttl_seconds


def test_all_status_covers_every_spec():
    assert {s["name"] for s in bs.all_status()} == {s.name for s in bs.all_specs()}


# ── login_enabled gate ────────────────────────────────────────────────────────

def test_login_enabled_default(monkeypatch):
    monkeypatch.delenv("BROWSER_LOGIN_ENABLED", raising=False)
    assert bs.login_enabled() is True


def test_login_disabled(monkeypatch):
    monkeypatch.setenv("BROWSER_LOGIN_ENABLED", "0")
    assert bs.login_enabled() is False


# ── start_login lifecycle (injected runner, no browser) ───────────────────────

def test_start_login_unknown_raises():
    with pytest.raises(bs.LoginError, match="Unknown"):
        bs.start_login("nope", runner=lambda *a: {"ok": True})


def test_start_login_disabled_raises(monkeypatch):
    monkeypatch.setenv("BROWSER_LOGIN_ENABLED", "0")
    with pytest.raises(bs.LoginError, match="disabled"):
        bs.start_login("tasker", runner=lambda *a: {"ok": True})


def test_start_login_success(monkeypatch):
    monkeypatch.setenv("BROWSER_LOGIN_ENABLED", "1")

    def runner(spec, on_progress, timeout):
        on_progress("halfway")
        return {"ok": True, "message": "saved", "state_path": "/x/state.json"}

    task = bs.start_login("tasker", runner=runner)
    assert task["status"] == "running"
    assert _wait_until(lambda: (bs.login_status("tasker") or {}).get("status") == "succeeded")
    done = bs.login_status("tasker")
    assert done["message"] == "saved"
    assert done["state_path"] == "/x/state.json"
    assert done["finished_at"] is not None


def test_start_login_failure_result(monkeypatch):
    monkeypatch.setenv("BROWSER_LOGIN_ENABLED", "1")
    task = bs.start_login(
        "tasker", runner=lambda *a: {"ok": False, "message": "timed out"}
    )
    assert task["status"] == "running"
    assert _wait_until(lambda: (bs.login_status("tasker") or {}).get("status") == "failed")
    assert bs.login_status("tasker")["message"] == "timed out"


def test_start_login_runner_exception_marked_failed(monkeypatch):
    monkeypatch.setenv("BROWSER_LOGIN_ENABLED", "1")

    def boom(*_a):
        raise RuntimeError("kaboom")

    bs.start_login("tasker", runner=boom)
    assert _wait_until(lambda: (bs.login_status("tasker") or {}).get("status") == "failed")
    assert "kaboom" in bs.login_status("tasker")["message"]


def test_start_login_rejects_concurrent(monkeypatch):
    monkeypatch.setenv("BROWSER_LOGIN_ENABLED", "1")
    release = {"go": False}

    def slow(spec, on_progress, timeout):
        while not release["go"]:
            time.sleep(0.01)
        return {"ok": True, "message": "done"}

    bs.start_login("tasker", runner=slow)
    try:
        with pytest.raises(bs.LoginError, match="already in progress"):
            bs.start_login("tasker", runner=slow)
    finally:
        release["go"] = True
    assert _wait_until(lambda: (bs.login_status("tasker") or {}).get("status") == "succeeded")


def test_status_reflects_in_progress_login(monkeypatch):
    monkeypatch.setenv("BROWSER_LOGIN_ENABLED", "1")
    release = {"go": False}

    def slow(spec, on_progress, timeout):
        while not release["go"]:
            time.sleep(0.01)
        return {"ok": True, "message": "done"}

    bs.start_login("tasker", runner=slow)
    try:
        st = bs.session_status("tasker")
        assert st["login_in_progress"] is True
    finally:
        release["go"] = True
    assert _wait_until(lambda: (bs.login_status("tasker") or {}).get("status") == "succeeded")
