"""DESKTOP_MODE single-admin auto-authentication (Electron desktop app).

The packaged desktop app runs as one local user with no login screen: setting
DESKTOP_MODE=1 makes every request auto-authenticate as the seeded admin
(src.auth.current_user falls back to _desktop_admin). These tests pin that
behavior — and, crucially, that it stays OFF by default so the web deployment
still enforces login. See doc/electron-desktop-app-design.md.
"""

import pytest


@pytest.mark.asyncio
async def test_desktop_mode_off_by_default_still_401(anon_client, seed_admin, monkeypatch):
    """Without DESKTOP_MODE, an anonymous request is rejected (web deploy default)."""
    monkeypatch.delenv("DESKTOP_MODE", raising=False)
    resp = await anon_client.get("/api/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_desktop_mode_auto_authenticates_as_admin(anon_client, seed_admin, monkeypatch):
    """With DESKTOP_MODE=1, no cookie needed — served as the seeded admin."""
    monkeypatch.setenv("DESKTOP_MODE", "1")
    resp = await anon_client.get("/api/auth/me")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == seed_admin.username
    assert body["is_admin"] is True


@pytest.mark.asyncio
async def test_desktop_mode_opens_gated_routes(anon_client, seed_admin, monkeypatch):
    """A require_user-gated route is reachable with no login under DESKTOP_MODE."""
    monkeypatch.setenv("DESKTOP_MODE", "1")
    resp = await anon_client.get("/api/schedules")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_desktop_mode_no_admin_seeded_returns_none(anon_client, monkeypatch):
    """DESKTOP_MODE with no admin in the DB must not crash — just stays unauthenticated."""
    # No seed_admin fixture here → empty users table.
    monkeypatch.setenv("DESKTOP_MODE", "1")
    resp = await anon_client.get("/api/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_desktop_mode_ignores_inactive_admin(anon_client, test_engine, monkeypatch):
    """A disabled admin is not used to auto-authenticate."""
    from sqlmodel import Session

    from src.auth import hash_password
    from src.models import User

    with Session(test_engine) as s:
        s.add(
            User(
                username="disabled-admin",
                password_hash=hash_password("x"),
                is_admin=True,
                is_active=False,
                allowed_automations="*",
            )
        )
        s.commit()

    monkeypatch.setenv("DESKTOP_MODE", "1")
    resp = await anon_client.get("/api/auth/me")
    assert resp.status_code == 401
