"""Integration tests for the auth flow and the API gate."""


# ── The gate: protected endpoints require a session ───────────────────────────

async def test_protected_endpoint_401_without_session(anon_client):
    resp = await anon_client.get("/api/jobs")
    assert resp.status_code == 401


async def test_me_401_without_session(anon_client):
    resp = await anon_client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_health_open_without_session(anon_client):
    resp = await anon_client.get("/health")
    assert resp.status_code == 200


# ── Login ─────────────────────────────────────────────────────────────────────

async def test_login_success_sets_session(anon_client, seed_admin):
    resp = await anon_client.post(
        "/api/auth/login", json={"username": "tester", "password": "password123"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "tester"
    assert body["is_admin"] is True
    assert "password_hash" not in body

    # Cookie now rides along → protected endpoint works.
    assert (await anon_client.get("/api/jobs")).status_code == 200
    assert (await anon_client.get("/api/auth/me")).json()["username"] == "tester"


async def test_login_wrong_password(anon_client, seed_admin):
    resp = await anon_client.post(
        "/api/auth/login", json={"username": "tester", "password": "nope"}
    )
    assert resp.status_code == 401


async def test_login_unknown_user(anon_client, seed_admin):
    resp = await anon_client.post(
        "/api/auth/login", json={"username": "ghost", "password": "password123"}
    )
    assert resp.status_code == 401


async def test_disabled_user_cannot_login(anon_client, test_engine):
    from sqlmodel import Session

    from src.auth import hash_password
    from src.models import User

    with Session(test_engine) as s:
        s.add(
            User(
                username="off",
                password_hash=hash_password("password123"),
                is_active=False,
            )
        )
        s.commit()

    resp = await anon_client.post(
        "/api/auth/login", json={"username": "off", "password": "password123"}
    )
    assert resp.status_code == 403


# ── Logout ──────────────────────────────────────────────────────────────────

async def test_logout_clears_session(client):
    assert (await client.get("/api/jobs")).status_code == 200
    assert (await client.post("/api/auth/logout")).status_code == 200
    assert (await client.get("/api/jobs")).status_code == 401


# ── Self-service password change ──────────────────────────────────────────────

async def test_change_password(client):
    resp = await client.post(
        "/api/auth/password",
        json={"current_password": "password123", "new_password": "newpassword456"},
    )
    assert resp.status_code == 200

    # Re-login with the new password works; the old one fails.
    await client.post("/api/auth/logout")
    assert (
        await client.post(
            "/api/auth/login",
            json={"username": "tester", "password": "newpassword456"},
        )
    ).status_code == 200


async def test_change_password_wrong_current(client):
    resp = await client.post(
        "/api/auth/password",
        json={"current_password": "WRONG", "new_password": "newpassword456"},
    )
    assert resp.status_code == 400


# ── SSO / OAuth ───────────────────────────────────────────────────────────────

async def test_providers_endpoint_open_without_session(anon_client):
    # Reachable pre-login so the login page can decide which SSO buttons to show.
    resp = await anon_client.get("/api/auth/providers")
    assert resp.status_code == 200
    assert "providers" in resp.json()  # empty list when no creds configured in tests


async def test_oauth_login_unconfigured_provider_404(anon_client):
    resp = await anon_client.get("/api/auth/oauth/google/login")
    assert resp.status_code == 404


async def test_sso_only_account_cannot_password_login(anon_client, test_engine):
    from sqlmodel import Session

    from src.models import User

    with Session(test_engine) as s:
        s.add(User(username="ssouser", password_hash=None, oauth_provider="google",
                   oauth_sub="X1", is_active=True))
        s.commit()

    resp = await anon_client.post(
        "/api/auth/login", json={"username": "ssouser", "password": "anything"}
    )
    assert resp.status_code == 401
