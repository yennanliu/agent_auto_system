"""Secure defaults (E10): the startup guard and the session-cookie Secure policy.

Server (non-desktop) mode must refuse to boot on the shipped admin/admin account
or the public dev APP_SECRET; desktop mode and the explicit dev opt-out must not.
"""

import logging

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session, select
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from src.auth import hash_password, verify_password
from src.models import User
from src.security import (
    DEV_SECRET,
    InsecureDefaultsError,
    SessionCookieMiddleware,
    enforce_secure_defaults,
    find_insecure_defaults,
    session_cookie_secure_mode,
)

GOOD = {"APP_SECRET": "a-long-random-secret-value", "ADMIN_PASSWORD": "s3cure-pass"}


def _add_user(engine, username="admin", password="admin", **kw):
    with Session(engine) as s:
        s.add(User(
            username=username,
            password_hash=hash_password(password) if password else None,
            is_admin=True,
            is_active=kw.get("is_active", True),
            allowed_automations="*",
        ))
        s.commit()


def _password_ok(engine, username, password):
    with Session(engine) as s:
        user = s.exec(select(User).where(User.username == username)).one()
        return verify_password(password, user.password_hash)


# ── startup guard: fresh database ─────────────────────────────────────────────

@pytest.mark.parametrize("env", [
    {},
    {"APP_SECRET": "", "ADMIN_PASSWORD": ""},
    {"APP_SECRET": DEV_SECRET, "ADMIN_PASSWORD": "admin"},
])
def test_server_mode_refuses_on_defaults(test_engine, env):
    with pytest.raises(InsecureDefaultsError) as exc:
        enforce_secure_defaults(test_engine, env)
    msg = str(exc.value)
    # Actionable: names every variable to set, plus the dev escape hatch.
    assert "APP_SECRET" in msg
    assert "ADMIN_PASSWORD" in msg
    assert "ALLOW_INSECURE_DEFAULTS" in msg


def test_server_mode_refuses_on_dev_secret_alone(test_engine):
    env = {**GOOD, "APP_SECRET": DEV_SECRET}
    with pytest.raises(InsecureDefaultsError) as exc:
        enforce_secure_defaults(test_engine, env)
    assert "APP_SECRET" in str(exc.value)
    assert "ADMIN_PASSWORD" not in str(exc.value)


def test_server_mode_refuses_on_default_password_alone(test_engine):
    env = {"APP_SECRET": GOOD["APP_SECRET"]}
    with pytest.raises(InsecureDefaultsError) as exc:
        enforce_secure_defaults(test_engine, env)
    assert "ADMIN_PASSWORD" in str(exc.value)
    assert "APP_SECRET is" not in str(exc.value)


def test_server_mode_starts_with_real_secrets(test_engine):
    enforce_secure_defaults(test_engine, GOOD)  # no raise
    assert find_insecure_defaults(test_engine, GOOD) == []


def test_desktop_mode_starts_with_zero_config(test_engine):
    enforce_secure_defaults(test_engine, {"DESKTOP_MODE": "1"})


def test_desktop_mode_must_be_exactly_1(test_engine):
    with pytest.raises(InsecureDefaultsError):
        enforce_secure_defaults(test_engine, {"DESKTOP_MODE": "0"})


def test_opt_out_starts_and_logs_loudly(test_engine, caplog):
    with caplog.at_level(logging.CRITICAL, logger="src.security"):
        enforce_secure_defaults(test_engine, {"ALLOW_INSECURE_DEFAULTS": "1"})
    assert any(
        r.levelno == logging.CRITICAL and "INSECURE DEFAULTS" in r.getMessage()
        and "APP_SECRET" in r.getMessage() and "ADMIN_PASSWORD" in r.getMessage()
        for r in caplog.records
    )


def test_opt_out_requires_a_truthy_value(test_engine):
    with pytest.raises(InsecureDefaultsError):
        enforce_secure_defaults(test_engine, {"ALLOW_INSECURE_DEFAULTS": "0"})


# ── startup guard: existing database ──────────────────────────────────────────

def test_existing_default_admin_refuses_while_env_is_default(test_engine):
    _add_user(test_engine)  # admin / admin, e.g. seeded by an older version
    with pytest.raises(InsecureDefaultsError) as exc:
        enforce_secure_defaults(test_engine, {"APP_SECRET": GOOD["APP_SECRET"]})
    assert "'admin'" in str(exc.value)
    assert "ADMIN_PASSWORD" in str(exc.value)
    assert _password_ok(test_engine, "admin", "admin")  # nothing was changed


def test_existing_default_admin_is_moved_to_admin_password(test_engine):
    """Setting ADMIN_PASSWORD is a real fix on an existing DB, not only a fresh one."""
    _add_user(test_engine)
    enforce_secure_defaults(test_engine, GOOD)
    assert _password_ok(test_engine, "admin", GOOD["ADMIN_PASSWORD"])
    assert not _password_ok(test_engine, "admin", "admin")


def test_password_changed_in_admin_page_passes_with_default_env(test_engine):
    """ADMIN_PASSWORD only seeds; a since-changed password must not block startup."""
    _add_user(test_engine, password="changed-in-ui")
    enforce_secure_defaults(test_engine, {"APP_SECRET": GOOD["APP_SECRET"]})
    assert _password_ok(test_engine, "admin", "changed-in-ui")


def test_custom_admin_username_is_checked_and_others_untouched(test_engine):
    _add_user(test_engine, username="root")            # default password
    _add_user(test_engine, username="bob", password="bobs-own-pass")
    env = {**GOOD, "ADMIN_USERNAME": "root"}
    with pytest.raises(InsecureDefaultsError):
        enforce_secure_defaults(test_engine, {"APP_SECRET": GOOD["APP_SECRET"], "ADMIN_USERNAME": "root"})
    enforce_secure_defaults(test_engine, env)
    assert _password_ok(test_engine, "root", GOOD["ADMIN_PASSWORD"])
    assert _password_ok(test_engine, "bob", "bobs-own-pass")


def test_inactive_and_sso_only_accounts_do_not_block(test_engine):
    _add_user(test_engine, username="admin", is_active=False)   # disabled default account
    _add_user(test_engine, username="sso", password=None)       # no password login
    enforce_secure_defaults(test_engine, {"APP_SECRET": GOOD["APP_SECRET"]})
    assert _password_ok(test_engine, "admin", "admin")  # inactive account left alone


# ── lifespan wiring ───────────────────────────────────────────────────────────

async def test_lifespan_refuses_before_seeding(mocker, test_engine, monkeypatch):
    import src.main as main

    for var in ("DESKTOP_MODE", "ALLOW_INSECURE_DEFAULTS"):
        monkeypatch.setenv(var, "")
    monkeypatch.setenv("APP_SECRET", DEV_SECRET)
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    mocker.patch.object(main._tel, "setup")
    mocker.patch("src.automation.harness.langfuse_tracer.get_client")
    mocker.patch("src.automation.harness.langfuse_tracer.ensure_score_configs")
    mocker.patch.object(main, "init_db")
    mocker.patch.object(main, "get_engine", return_value=test_engine)
    seed = mocker.patch.object(main, "_seed_admin")

    with pytest.raises(InsecureDefaultsError):
        async with main.lifespan(main.app):
            pytest.fail("startup should have been refused")
    seed.assert_not_called()  # no admin/admin row is left behind by a refused boot


def test_seed_treats_blank_admin_password_as_unset(mocker, test_engine, monkeypatch):
    import src.main as main

    monkeypatch.setenv("ADMIN_USERNAME", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    mocker.patch.object(main, "get_engine", return_value=test_engine)
    main._seed_admin()
    assert _password_ok(test_engine, "admin", "admin")
    assert not _password_ok(test_engine, "admin", "")


# ── session cookie Secure flag ────────────────────────────────────────────────

@pytest.mark.parametrize("raw,mode", [
    (None, "auto"), ("", "auto"), ("auto", "auto"), ("bogus", "auto"),
    ("1", "always"), ("true", "always"), ("Always", "always"),
    ("0", "never"), ("false", "never"), ("never", "never"),
])
def test_session_cookie_secure_mode(raw, mode):
    env = {} if raw is None else {"SESSION_COOKIE_SECURE": raw}
    assert session_cookie_secure_mode(env) == mode


async def _login_cookie(mode: str, base_url: str) -> str:
    async def login(request):
        request.session["user_id"] = 1
        return PlainTextResponse("ok")

    app = SessionCookieMiddleware(
        Starlette(routes=[Route("/login", login)]), secret_key="k", mode=mode
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=base_url) as c:
        resp = await c.get("/login")
    return resp.headers["set-cookie"].lower()


@pytest.mark.parametrize("mode,base_url,secure", [
    ("always", "http://test", True),    # TLS terminated upstream, explicit setting
    ("always", "https://test", True),
    ("auto", "https://test", True),     # request arrived over TLS / trusted X-Forwarded-Proto
    ("auto", "http://test", False),     # plain-HTTP dev / desktop loopback keeps working
    ("never", "https://test", False),
])
async def test_session_cookie_secure_flag(mode, base_url, secure):
    cookie = await _login_cookie(mode, base_url)
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert ("secure" in cookie.split("; ")) is secure


def test_app_uses_the_tls_aware_session_middleware():
    from src.main import app

    assert any(m.cls is SessionCookieMiddleware for m in app.user_middleware)
