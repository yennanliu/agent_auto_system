"""Admin-only API: user/permission management, LLM keys, automation toggles.

Every route depends on ``require_admin`` (declared at include time in main.py and
re-asserted here so the router is safe to mount anywhere).
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, func, select

from src import settings_store
from src.auth import hash_password, require_admin
from src.database import get_session
from src.models import User

router = APIRouter(dependencies=[Depends(require_admin)])


# ── Schemas ───────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False
    allowed_automations: list[str] | None = None  # None → none; ["*"] → all


class UserUpdate(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None
    allowed_automations: list[str] | None = None


class PasswordReset(BaseModel):
    new_password: str


class LLMKeyUpdate(BaseModel):
    api_key: str  # empty/whitespace → clear the DB override


class AutomationsUpdate(BaseModel):
    enabled: list[str]


class EvalJudgeUpdate(BaseModel):
    provider: str | None = None  # falsy → revert to env/default ("Auto")
    model: str | None = None     # falsy → the provider's default model


# ── Helpers ───────────────────────────────────────────────────────────────────

def _public(user: User) -> dict:
    raw = (user.allowed_automations or "").strip()
    allowed: str | list = "*" if raw == "*" else []
    if raw != "*":
        try:
            allowed = json.loads(raw)
        except (ValueError, TypeError):
            allowed = []
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "allowed_automations": allowed,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def _encode_allowlist(value: list[str] | None) -> str:
    """["*"] (or "*" anywhere) → wildcard; otherwise a JSON list of known types."""
    if value is None:
        return "[]"
    if "*" in value:
        return "*"
    cleaned = [t for t in value if t in settings_store.ALL_AUTOMATIONS]
    return json.dumps(cleaned)


def _active_admin_count(session: Session, exclude_id: int | None = None) -> int:
    stmt = select(func.count()).select_from(User).where(
        User.is_admin == True, User.is_active == True  # noqa: E712
    )
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return session.exec(stmt).one()


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/admin/users")
def list_users(session: Session = Depends(get_session)):
    users = session.exec(select(User).order_by(User.created_at)).all()
    return [_public(u) for u in users]


@router.post("/admin/users", status_code=201)
def create_user(data: UserCreate, session: Session = Depends(get_session)):
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    exists = session.exec(select(User).where(User.username == data.username)).first()
    if exists:
        raise HTTPException(status_code=409, detail="Username already exists")
    user = User(
        username=data.username.strip(),
        password_hash=hash_password(data.password),
        is_admin=data.is_admin,
        is_active=True,
        allowed_automations=_encode_allowlist(data.allowed_automations),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return _public(user)


@router.patch("/admin/users/{user_id}")
def update_user(
    user_id: int,
    data: UserUpdate,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin),
):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Guard against locking out the last admin (by demotion or deactivation).
    demoting = data.is_admin is False and user.is_admin
    deactivating = data.is_active is False and user.is_active
    if (demoting or deactivating) and _active_admin_count(session, exclude_id=user.id) == 0:
        raise HTTPException(status_code=400, detail="Cannot remove the last active admin")

    if data.is_admin is not None:
        user.is_admin = data.is_admin
    if data.is_active is not None:
        user.is_active = data.is_active
    if data.allowed_automations is not None:
        user.allowed_automations = _encode_allowlist(data.allowed_automations)

    session.add(user)
    session.commit()
    session.refresh(user)
    return _public(user)


@router.post("/admin/users/{user_id}/password")
def reset_password(
    user_id: int, data: PasswordReset, session: Session = Depends(get_session)
):
    if len(data.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(data.new_password)
    session.add(user)
    session.commit()
    return {"ok": True}


@router.delete("/admin/users/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin),
):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    if user.is_admin and user.is_active and _active_admin_count(session, exclude_id=user.id) == 0:
        raise HTTPException(status_code=400, detail="Cannot delete the last active admin")
    session.delete(user)
    session.commit()


# ── LLM API keys ──────────────────────────────────────────────────────────────

@router.get("/admin/llm-keys")
def list_llm_keys():
    from src.automation.harness.provider import _CATALOG

    return [
        {
            "provider": name,
            "env": cfg["env"],
            "models": cfg["models"],
            **settings_store.llm_key_status(name, cfg["env"]),
        }
        for name, cfg in _CATALOG.items()
    ]


@router.put("/admin/llm-keys/{provider}")
def set_llm_key(provider: str, data: LLMKeyUpdate):
    from src.automation.harness.provider import _CATALOG

    if provider not in _CATALOG:
        raise HTTPException(status_code=404, detail="Unknown provider")
    key = data.api_key.strip()
    if key:
        settings_store.set_llm_key(provider, key)
    else:
        settings_store.clear_llm_key(provider)  # revert to env fallback
    cfg = _CATALOG[provider]
    return {"provider": provider, **settings_store.llm_key_status(provider, cfg["env"])}


# ── Automation toggles ────────────────────────────────────────────────────────

@router.get("/admin/automations")
def get_automations():
    return {
        "all": settings_store.ALL_AUTOMATIONS,
        "enabled": settings_store.get_enabled_automations(),
    }


@router.put("/admin/automations")
def set_automations(data: AutomationsUpdate):
    settings_store.set_enabled_automations(data.enabled)
    return {
        "all": settings_store.ALL_AUTOMATIONS,
        "enabled": settings_store.get_enabled_automations(),
    }


# ── Evaluation judge (LLM-as-judge model) ─────────────────────────────────────

def _eval_judge_payload() -> dict:
    from src.automation.harness.evaluator import _DEFAULT_JUDGE
    from src.automation.harness.provider import _CATALOG

    provider, model = settings_store.get_eval_judge()
    return {
        "provider": provider,  # None → "Auto" (env/default)
        "model": model,
        "default": {"provider": _DEFAULT_JUDGE[0], "model": _DEFAULT_JUDGE[1]},
        "providers": {name: cfg["models"] for name, cfg in _CATALOG.items()},
    }


@router.get("/admin/eval-judge")
def get_eval_judge():
    return _eval_judge_payload()


@router.put("/admin/eval-judge")
def set_eval_judge(data: EvalJudgeUpdate):
    from src.automation.harness.provider import _CATALOG

    provider = (data.provider or "").strip() or None
    if provider and provider not in _CATALOG:
        raise HTTPException(status_code=404, detail="Unknown provider")
    model = (data.model or "").strip() or None
    if provider and model and model not in _CATALOG[provider]["models"]:
        raise HTTPException(status_code=400, detail="Unknown model for provider")
    settings_store.set_eval_judge(provider, model)
    return _eval_judge_payload()
