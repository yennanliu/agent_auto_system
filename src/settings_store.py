"""Runtime-configurable settings backed by the ``Setting`` table.

Two things admins configure at runtime live here:

* **LLM API keys** — stored encrypted (Fernet, key derived from ``APP_SECRET``).
  ``get_llm_key`` prefers the DB value, falling back to the environment variable,
  so existing ``.env`` keys keep working untouched.
* **Enabled automations** — which job types may be run system-wide. Unset means
  "all enabled", so upgrades don't disable anything.
"""

import base64
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from src.database import get_engine
from src.models import Setting

logger = logging.getLogger(__name__)

# Canonical automation job types. Kept in sync by hand with executor._FLOW_MAP
# (+ "pipeline") and ui/app.js ALL_TYPES — same pattern as the other catalogs.
ALL_AUTOMATIONS = [
    "google_form_fill",
    "web_scraper",
    "hacker_news_digest",
    "x_scraper",
    "email_sender",
    "google_sheet_reader",
    "shopee_seller_scraper",
    "profit_health_check",
    "tasker_apply",
    "tw104_apply",
    "email_collect",
    "pipeline",
]

_ENABLED_KEY = "enabled_automations"
_LLM_KEY_PREFIX = "llm_key:"
_EVAL_JUDGE_KEY = "eval_judge"


# ── Generic key/value access ──────────────────────────────────────────────────

def get_setting(key: str) -> str | None:
    # Resilient to a missing table / unconfigured DB (e.g. the harness used
    # outside the web app): callers fall back to environment defaults.
    try:
        with Session(get_engine()) as s:
            row = s.get(Setting, key)
            return row.value if row else None
    except SQLAlchemyError:
        logger.debug("Setting table unavailable while reading %r; treating as unset", key)
        return None


def set_setting(key: str, value: str) -> None:
    with Session(get_engine()) as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
            row.updated_at = datetime.now(UTC)
        else:
            row = Setting(key=key, value=value)
        s.add(row)
        s.commit()


def delete_setting(key: str) -> None:
    with Session(get_engine()) as s:
        row = s.get(Setting, key)
        if row:
            s.delete(row)
            s.commit()


# ── Encryption ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    # APP_SECRET is static for the process lifetime, so derive the key once.
    secret = os.getenv("APP_SECRET") or "dev-insecure-change-me"
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def _encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def _decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        # Wrong/rotated APP_SECRET, or a non-token value — treat as unset.
        return None


# ── LLM API keys ──────────────────────────────────────────────────────────────

def get_llm_key(provider: str, env_name: str) -> str | None:
    """Resolve a provider's API key: DB (decrypted) first, then environment."""
    stored = get_setting(_LLM_KEY_PREFIX + provider)
    if stored:
        decrypted = _decrypt(stored)
        if decrypted:
            return decrypted
    return os.getenv(env_name)


def set_llm_key(provider: str, plaintext: str) -> None:
    set_setting(_LLM_KEY_PREFIX + provider, _encrypt(plaintext))


def clear_llm_key(provider: str) -> None:
    """Remove the DB override; resolution falls back to the environment."""
    delete_setting(_LLM_KEY_PREFIX + provider)


def llm_key_status(provider: str, env_name: str) -> dict:
    """Where the key comes from and a masked preview — never the plaintext."""
    stored = get_setting(_LLM_KEY_PREFIX + provider)
    if stored and (decrypted := _decrypt(stored)):
        return {"configured": True, "source": "db", "masked": _mask(decrypted)}
    env_val = os.getenv(env_name)
    if env_val:
        return {"configured": True, "source": "env", "masked": _mask(env_val)}
    return {"configured": False, "source": None, "masked": None}


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


# ── Enabled automations ───────────────────────────────────────────────────────

def get_enabled_automations() -> list[str]:
    """The set of enabled job types. Unset → all enabled (safe upgrade default)."""
    raw = get_setting(_ENABLED_KEY)
    if raw is None:
        return list(ALL_AUTOMATIONS)
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return list(ALL_AUTOMATIONS)
    # Only return job types we still know about.
    return [t for t in stored if t in ALL_AUTOMATIONS]


def set_enabled_automations(job_types: list[str]) -> None:
    cleaned = [t for t in job_types if t in ALL_AUTOMATIONS]
    set_setting(_ENABLED_KEY, json.dumps(cleaned))


def is_automation_enabled(job_type: str) -> bool:
    return job_type in get_enabled_automations()


# ── Evaluation judge (LLM-as-judge model) ─────────────────────────────────────

def get_eval_judge() -> tuple[str | None, str | None]:
    """Admin-configured judge as (provider, model). ``(None, None)`` means unset —
    the evaluator then uses its env override or code default. A provider with a
    ``None`` model means "that provider's default model"."""
    raw = get_setting(_EVAL_JUDGE_KEY)
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
        return (data.get("provider") or None), (data.get("model") or None)
    except (ValueError, TypeError):
        return None, None


def set_eval_judge(provider: str | None, model: str | None) -> None:
    """Set (or, with a falsy provider, clear → revert to env/default) the judge."""
    if not provider:
        delete_setting(_EVAL_JUDGE_KEY)
        return
    set_setting(_EVAL_JUDGE_KEY, json.dumps({"provider": provider, "model": model or None}))
