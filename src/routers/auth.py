import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from src.auth import current_user, hash_password, require_user, verify_password
from src.database import get_session
from src.models import User

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


def _public(user: User) -> dict:
    """User shape safe to send to the browser (no password hash)."""
    from src import settings_store

    raw = (user.allowed_automations or "").strip()
    if raw == "*":
        allowed = "*"  # wildcard: every automation
    else:
        try:
            allowed = json.loads(raw)
        except (ValueError, TypeError):
            allowed = []
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "oauth_provider": user.oauth_provider,  # None for password accounts
        "allowed_automations": allowed,  # "*" or list of job_type
        "enabled_automations": settings_store.get_enabled_automations(),  # global
    }


@router.post("/auth/login")
def login(
    data: LoginRequest, request: Request, session: Session = Depends(get_session)
):
    user = session.exec(select(User).where(User.username == data.username)).first()
    # SSO-only accounts have no password_hash and can't log in with a password.
    if not user or not user.password_hash or not verify_password(
        data.password, user.password_hash
    ):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    request.session["user_id"] = user.id
    user.last_login_at = datetime.now(UTC)
    session.add(user)
    session.commit()
    return _public(user)


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/auth/me")
def me(user: User | None = Depends(current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return _public(user)


@router.post("/auth/password")
def change_password(
    data: PasswordChange,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not user.password_hash:
        raise HTTPException(
            status_code=400,
            detail="This account signs in via SSO and has no password to change",
        )
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(data.new_password) < 8:
        raise HTTPException(
            status_code=400, detail="New password must be at least 8 characters"
        )
    user.password_hash = hash_password(data.new_password)
    session.add(user)
    session.commit()
    return {"ok": True}
