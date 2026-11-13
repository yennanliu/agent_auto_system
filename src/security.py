"""Secure-defaults startup guard and the session-cookie ``Secure`` policy.

Two things live here so ``main.py`` stays a thin wiring file:

* :func:`enforce_secure_defaults` — a server (non-desktop) deployment refuses to
  boot while it would still be reachable with the shipped ``admin``/``admin``
  account or sign its session cookies with the public dev ``APP_SECRET``.
* :class:`SessionCookieMiddleware` — Starlette's ``SessionMiddleware`` with the
  cookie's ``Secure`` flag driven by ``SESSION_COOKIE_SECURE`` / the request scheme.

Escape hatches (both explicit):

* ``DESKTOP_MODE=1`` — the Electron app. It binds loopback only and auto-logs-in
  the single local admin, so the checks are skipped and it needs zero config.
* ``ALLOW_INSECURE_DEFAULTS=1`` — local development. Starts anyway and logs every
  open problem at CRITICAL on each boot.
"""

import logging
import os
from collections.abc import Mapping

from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

DEV_SECRET = "dev-insecure-change-me"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class InsecureDefaultsError(RuntimeError):
    """Raised at startup when a server deployment still runs on shipped defaults."""


def is_desktop_mode(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("DESKTOP_MODE") == "1"


def insecure_defaults_allowed(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("ALLOW_INSECURE_DEFAULTS") or "").strip().lower() in _TRUE


def _default_password_accounts(engine, usernames: set[str]) -> list:
    """Active password accounts among ``usernames`` that still accept ``admin``."""
    from sqlmodel import Session, col, select

    from src.auth import verify_password
    from src.models import User

    with Session(engine) as session:
        users = session.exec(select(User).where(col(User.username).in_(usernames))).all()
        return [
            u for u in users
            if u.is_active and u.password_hash
            and verify_password(DEFAULT_ADMIN_PASSWORD, u.password_hash)
        ]


def _has_users(engine) -> bool:
    from sqlmodel import Session, select

    from src.models import User

    with Session(engine) as session:
        return session.exec(select(User)).first() is not None


def _rotate_password(engine, user_id: int, password: str) -> None:
    from sqlmodel import Session

    from src.auth import hash_password
    from src.models import User

    with Session(engine) as session:
        user = session.get(User, user_id)
        user.password_hash = hash_password(password)
        session.add(user)
        session.commit()


def find_insecure_defaults(engine, env: Mapping[str, str] | None = None) -> list[str]:
    """Return one actionable message per insecure default still in effect.

    Read-only apart from one deliberate repair: an account that still has the
    default password is moved to ``ADMIN_PASSWORD`` when the operator has set a
    real one. Seeding only happens on an empty users table, so without this an
    existing deployment could never clear the problem by setting the env var.
    """
    env = os.environ if env is None else env
    problems: list[str] = []

    secret = env.get("APP_SECRET") or ""
    if not secret or secret == DEV_SECRET:
        problems.append(
            "APP_SECRET is "
            + ("unset" if not secret else "the public dev value")
            + " — session cookies would be signed (and stored API keys encrypted) with a "
            "secret anyone can read in the repo. Set APP_SECRET to a long random value, "
            'e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`.'
        )

    admin_username = env.get("ADMIN_USERNAME") or DEFAULT_ADMIN_USERNAME
    admin_password = env.get("ADMIN_PASSWORD") or DEFAULT_ADMIN_PASSWORD
    password_is_default = admin_password == DEFAULT_ADMIN_PASSWORD

    if not _has_users(engine):
        if password_is_default:
            problems.append(
                "ADMIN_PASSWORD is unset or 'admin' — the first admin "
                f"('{admin_username}') would be seeded with the default password. "
                "Set ADMIN_PASSWORD to a strong value."
            )
        return problems

    for user in _default_password_accounts(engine, {DEFAULT_ADMIN_USERNAME, admin_username}):
        if password_is_default:
            problems.append(
                f"Account '{user.username}' still has the default password 'admin'. "
                "Set ADMIN_PASSWORD to a strong value (it replaces the default password "
                "on the next start) or change the password in the Admin page."
            )
        else:
            _rotate_password(engine, user.id, admin_password)
            logger.warning(
                "Account '%s' still had the default password 'admin'; replaced it with "
                "ADMIN_PASSWORD.",
                user.username,
            )
    return problems


def enforce_secure_defaults(engine, env: Mapping[str, str] | None = None) -> None:
    """Refuse to start a server deployment that is still on shipped defaults.

    Call after the schema exists and before the admin seed. No-op in desktop mode;
    downgraded to a CRITICAL log with ``ALLOW_INSECURE_DEFAULTS=1``.
    """
    env = os.environ if env is None else env
    if is_desktop_mode(env):
        return

    problems = find_insecure_defaults(engine, env)
    if not problems:
        return

    listing = "\n".join(f"  - {p}" for p in problems)
    if insecure_defaults_allowed(env):
        logger.critical(
            "ALLOW_INSECURE_DEFAULTS=1 — starting with INSECURE DEFAULTS. "
            "Local development only; never expose this instance:\n%s",
            listing,
        )
        return

    message = (
        "Refusing to start with insecure defaults:\n"
        f"{listing}\n"
        "Fix the settings above (environment or .env) and restart. For local "
        "development only, set ALLOW_INSECURE_DEFAULTS=1 to start anyway."
    )
    logger.critical(message)
    raise InsecureDefaultsError(message)


def session_cookie_secure_mode(env: Mapping[str, str] | None = None) -> str:
    """``always`` | ``never`` | ``auto`` from ``SESSION_COOKIE_SECURE``.

    ``auto`` (the default) marks the cookie ``Secure`` on requests that arrived
    over HTTPS — directly, or via ``X-Forwarded-Proto`` from a proxy uvicorn
    trusts (``FORWARDED_ALLOW_IPS``).
    """
    env = os.environ if env is None else env
    raw = (env.get("SESSION_COOKIE_SECURE") or "auto").strip().lower()
    if raw in _TRUE or raw == "always":
        return "always"
    if raw in _FALSE or raw == "never":
        return "never"
    if raw != "auto":
        logger.warning("Unrecognised SESSION_COOKIE_SECURE=%r; using 'auto'.", raw)
    return "auto"


class SessionCookieMiddleware:
    """``SessionMiddleware`` whose ``Secure`` flag follows the TLS policy above.

    Starlette fixes ``https_only`` per middleware instance, so this holds one
    instance of each flavour and picks per request.
    """

    def __init__(self, app: ASGIApp, secret_key: str, mode: str | None = None, **kwargs) -> None:
        self.mode = mode or session_cookie_secure_mode()
        self._plain = SessionMiddleware(app, secret_key=secret_key, https_only=False, **kwargs)
        self._secure = SessionMiddleware(app, secret_key=secret_key, https_only=True, **kwargs)

    def _is_secure(self, scope: Scope) -> bool:
        if self.mode == "always":
            return True
        if self.mode == "never":
            return False
        return scope.get("scheme") in ("https", "wss")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        handler = self._secure if self._is_secure(scope) else self._plain
        await handler(scope, receive, send)
