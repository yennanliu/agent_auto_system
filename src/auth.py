"""Authentication: password hashing and session-based access dependencies.

Login stores the user id in a signed, HttpOnly session cookie (Starlette
``SessionMiddleware``, registered in ``main.py``). Request-time dependencies load
the active user from that session and gate access.
"""

import json
import os

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlmodel import Session, select

from src.database import get_session
from src.models import User

# bcrypt hashes at most the first 72 bytes of a password; truncate explicitly so
# longer inputs fail the same way on hash and verify instead of silently differing.
_MAX_PW_BYTES = 72


def _pw_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_MAX_PW_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_pw_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_pw_bytes(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _desktop_admin(session: Session) -> User | None:
    """The single local admin used to auto-authenticate in DESKTOP_MODE.

    The packaged desktop app (Electron) runs as one local user, so there is no
    login screen — every request is served as the seeded admin. Picks the
    lowest-id active admin (the seeded one). See doc/electron-desktop-app-design.md.
    """
    return session.exec(
        select(User)
        .where(User.is_admin == True, User.is_active == True)  # noqa: E712 (SQL bool)
        .order_by(User.id)
    ).first()


def current_user(
    request: Request, session: Session = Depends(get_session)
) -> User | None:
    """The logged-in, active user for this request, or None."""
    user_id = request.session.get("user_id")
    if not user_id:
        # Desktop single-admin mode: no login, auto-authenticate as the admin.
        if os.getenv("DESKTOP_MODE") == "1":
            return _desktop_admin(session)
        return None
    user = session.get(User, user_id)
    if not user or not user.is_active:
        return None
    return user


def require_user(user: User | None = Depends(current_user)) -> User:
    """Gate: 401 unless a valid, active user is logged in."""
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    """Gate: 403 unless the logged-in user is an admin."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


def user_allowed_automations(user: User) -> str | list[str]:
    """A user's allowlist: the "*" wildcard or a list of job types."""
    raw = (user.allowed_automations or "").strip()
    if raw == "*":
        return "*"
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def assert_can_run(user: User, job_type: str) -> None:
    """Raise 403 if ``user`` may not run ``job_type``.

    A globally disabled automation is blocked for everyone (admins re-enable it
    in the admin page to run it). Beyond that, admins bypass the per-user
    allowlist; regular users must have the job type in theirs.
    """
    from src.settings_store import is_automation_enabled

    if not is_automation_enabled(job_type):
        raise HTTPException(status_code=403, detail=f"Automation '{job_type}' is disabled")
    if user.is_admin:
        return
    allowed = user_allowed_automations(user)
    if allowed == "*" or job_type in allowed:
        return
    raise HTTPException(
        status_code=403, detail=f"You are not permitted to run '{job_type}'"
    )
