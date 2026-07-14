"""Pure, testable helpers around cron expressions (croniter).

Kept free of any DB / event-loop dependency so the scheduling logic can be unit
tested in isolation. The scheduler (``scheduler.py``) builds on these.
"""
from datetime import UTC, datetime

from croniter import croniter

# Common nickname macros → 5-field cron. croniter understands some of these
# natively, but normalizing up front keeps validation, display, and next-fire
# computation consistent regardless of croniter version.
_MACROS = {
    "@hourly":   "0 * * * *",
    "@daily":    "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly":   "0 0 * * 0",
    "@monthly":  "0 0 1 * *",
    "@yearly":   "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}


def normalize_cron(expr: str) -> str:
    """Trim, collapse whitespace, and expand @macros to a 5-field expression."""
    if expr is None:
        return ""
    cleaned = " ".join(str(expr).strip().split())
    return _MACROS.get(cleaned.lower(), cleaned)


def is_valid_cron(expr: str | None) -> bool:
    """True if ``expr`` is a cron expression croniter can parse (after macro expand)."""
    if not expr or not str(expr).strip():
        return False
    try:
        return bool(croniter.is_valid(normalize_cron(expr)))
    except (ValueError, TypeError):
        return False


def next_fire(expr: str, after: datetime | None = None) -> datetime:
    """Next fire time strictly after ``after`` (UTC-aware). Raises ValueError if invalid."""
    norm = normalize_cron(expr)
    if not is_valid_cron(norm):
        raise ValueError(f"Invalid cron expression: {expr!r}")
    base = after or datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    itr = croniter(norm, base)
    nxt = itr.get_next(datetime)
    # croniter may return a naive datetime depending on the base; pin to UTC.
    return nxt if nxt.tzinfo is not None else nxt.replace(tzinfo=UTC)
