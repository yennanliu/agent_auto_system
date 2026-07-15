"""Store + helpers for admin-authored, no-code automations (Phase 3G).

A ``CustomAutomation`` row becomes the job type ``custom:<slug>``. These run as a
single LLM agent with **no tools** (the safety boundary — no network/file/secret
access, no code execution), through the normal executor harness. Because the UI is
manifest-driven (Phase 2), a custom automation renders its picker tile and run form
with no extra front-end code.

Security posture (MVP): admin-only authoring, LLM-only, per-input length caps.
Broadening this (tool access, non-admin authoring) needs the security review the
RFC calls for — see doc/automation-extensibility-design.md §8.
"""
from __future__ import annotations

import hashlib
import json
import re

from sqlmodel import Session, select

from src.database import get_engine
from src.models import CustomAutomation

PREFIX = "custom:"
MAX_FIELDS = 8
MAX_INSTRUCTIONS = 4000
_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_ALLOWED_FIELD_TYPES = {"text", "textarea", "number"}

# Generic step graph for every custom automation (labels + log triggers that the
# executor / dynamic runner actually emit).
STEPS = [
    ["Start", "Starting"],
    ["Generate", "Custom agent"],
    ["Verify", "Validating result"],
    ["Evaluate", "Evaluation complete"],
    ["Done", "completed successfully"],
]


def slugify(name: str) -> str:
    """A URL-safe slug from a display name.

    Non-ASCII / CJK names (e.g. '利潤健檢') collapse to empty after stripping
    non-[a-z0-9_] chars, so fall back to a short stable hash of the name — the
    slug is internal (job_type = custom:<slug>); the display name is separate.
    """
    slug = _SLUG_RE.sub("_", name.strip().lower()).strip("_")[:40]
    if not slug:
        slug = "a" + hashlib.md5(name.strip().encode("utf-8")).hexdigest()[:10]
    return slug


def job_type_for(slug: str) -> str:
    return f"{PREFIX}{slug}"


def is_custom(job_type: str) -> bool:
    return job_type.startswith(PREFIX)


def _clean_fields(fields: list[dict]) -> list[dict]:
    """Validate/normalize the declared inputs (defensive — admin-authored)."""
    out = []
    seen: set[str] = set()
    for f in (fields or [])[:MAX_FIELDS]:
        name = _SLUG_RE.sub("_", str(f.get("name", "")).strip().lower()).strip("_")
        if not name or name in seen:
            continue  # skip empty and duplicate names (client reads the first only)
        seen.add(name)
        ftype = f.get("type") if f.get("type") in _ALLOWED_FIELD_TYPES else "text"
        out.append({
            "name": name,
            "label": str(f.get("label") or name)[:80],
            "type": ftype,
            "required": bool(f.get("required", True)),
        })
    return out


def list_all() -> list[CustomAutomation]:
    with Session(get_engine()) as s:
        return list(s.exec(select(CustomAutomation)))


def get_by_job_type(job_type: str) -> CustomAutomation | None:
    if not is_custom(job_type):
        return None
    slug = job_type[len(PREFIX):]
    with Session(get_engine()) as s:
        return s.exec(select(CustomAutomation).where(CustomAutomation.slug == slug)).first()


def is_enabled(job_type: str) -> bool:
    row = get_by_job_type(job_type)
    return bool(row and row.enabled)


def create(*, name: str, instructions: str, icon: str = "✨", description: str = "",
           output_hint: str = "", fields: list[dict] | None = None,
           temperature: float = 0.3, created_by: int | None = None) -> CustomAutomation:
    name = (name or "").strip()
    instructions = (instructions or "").strip()[:MAX_INSTRUCTIONS]
    if not name:
        raise ValueError("name is required")
    if not instructions:
        raise ValueError("instructions are required")
    slug = slugify(name)  # always non-empty (hash fallback for non-ASCII names)
    row = CustomAutomation(
        slug=slug, name=name[:80], icon=(icon or "✨")[:8],
        description=description.strip()[:200], instructions=instructions,
        output_hint=(output_hint or "").strip()[:500],
        fields_json=json.dumps(_clean_fields(fields or [])),
        temperature=min(max(float(temperature), 0.0), 1.0),
        created_by=created_by,
    )
    with Session(get_engine()) as s:
        if s.exec(select(CustomAutomation).where(CustomAutomation.slug == slug)).first():
            raise ValueError(f"a custom automation named like {slug!r} already exists")
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def delete(automation_id: int) -> bool:
    with Session(get_engine()) as s:
        row = s.get(CustomAutomation, automation_id)
        if not row:
            return False
        s.delete(row)
        s.commit()
        return True


def set_enabled(automation_id: int, enabled: bool) -> CustomAutomation | None:
    with Session(get_engine()) as s:
        row = s.get(CustomAutomation, automation_id)
        if not row:
            return None
        row.enabled = enabled
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def to_public(row: CustomAutomation) -> dict:
    return {
        "id": row.id, "slug": row.slug, "job_type": job_type_for(row.slug),
        "name": row.name, "icon": row.icon, "description": row.description,
        "instructions": row.instructions, "output_hint": row.output_hint,
        "fields": json.loads(row.fields_json or "[]"),
        "temperature": row.temperature, "enabled": row.enabled,
    }


def manifest_entries() -> list[dict]:
    """Serialize enabled custom automations into the browser manifest shape, so
    the picker + generic run form render them with no extra UI code."""
    entries = []
    for row in list_all():
        if not row.enabled:
            continue
        fields = json.loads(row.fields_json or "[]")
        entries.append({
            "job_type": job_type_for(row.slug),
            "name": row.name,
            "icon": row.icon or "✨",
            "desc": row.description or "Custom automation",
            "browser": False,
            "custom_ui": False,
            "name_template": row.name,
            "help_note": "",
            "steps": [list(s) for s in STEPS],
            "custom": True,
            "fields": [
                {"name": f["name"], "type": f.get("type", "text"),
                 "label": f.get("label", f["name"]),
                 "required": f.get("required", True), "default": None,
                 "min": None, "max": None, "placeholder": "", "help": "", "options": []}
                for f in fields
            ],
        })
    return entries
