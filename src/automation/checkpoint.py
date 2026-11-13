"""Per-run step checkpoints — the storage half of resumable runs.

A run keeps one JSON object in ``run.checkpoint`` mapping ``step_key → data``.
A flow (or the pipeline) calls :func:`save` after it finishes a unit of work and
:func:`load` when it starts, so a run re-launched after a restart (see
``src/automation/resume.py``) can skip what is already done.

Rules for callers:

* **Checkpoint after the work, never before.** A crash between "work done" and
  "checkpoint written" therefore *repeats* that unit on resume. That is only
  acceptable for work that is safe to repeat (reads, scrapes, pure computation),
  which is why ``AutomationSpec.resumable`` defaults to ``False`` and flows with
  outward side effects (applying to a job, sending mail) must stay that way.
* **Store a signature of the inputs** next to the data and ignore a checkpoint
  whose signature does not match (:func:`signature`). A resumed run reads its
  payload from the Job row, which may have been edited since the run started.
* Checkpointing is best-effort: :func:`save` and :func:`load` never raise into
  the flow. A lost checkpoint only costs redone work, never a failed run.
"""
import hashlib
import json
import logging
import threading

from sqlalchemy import text

logger = logging.getLogger(__name__)

# save() is read-modify-write; serialise writers in this process (flows run in
# worker threads). One run only ever has one writer, so this is uncontended.
_LOCK = threading.Lock()


def _engine():
    from src.database import get_engine
    return get_engine()


def signature(obj) -> str:
    """Stable short hash of JSON-able inputs, to tie a checkpoint to its payload."""
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def parse(raw: str | None) -> dict:
    """Decode a ``run.checkpoint`` column value; anything unusable is ``{}``."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def load(run_id: int) -> dict:
    """Every checkpoint saved for the run as ``{step_key: data}`` ({} if none)."""
    if not run_id:
        return {}
    try:
        with _engine().connect() as conn:
            row = conn.execute(
                text("SELECT checkpoint FROM run WHERE id = :id"), {"id": run_id}
            ).first()
        return parse(row[0]) if row else {}
    except Exception as exc:  # noqa: BLE001 — a failed load just means "start over"
        logger.warning("checkpoint.load failed for run_id=%s: %s", run_id, exc)
        return {}


def save(run_id: int, step_key: str, data, *, keep_prefix: str | None = None) -> bool:
    """Record ``data`` (JSON-able) under ``step_key``. Returns False if not stored.

    ``keep_prefix`` additionally drops, in the same write, every *other* key that
    does not start with that prefix. The pipeline uses it to discard a finished
    step's flow-level checkpoints atomically with recording the step's result,
    so the next step can never pick up a previous step's progress.
    """
    if not run_id:
        return False
    try:
        with _LOCK, _engine().connect() as conn:
            row = conn.execute(
                text("SELECT checkpoint FROM run WHERE id = :id"), {"id": run_id}
            ).first()
            if row is None:
                return False
            current = parse(row[0])
            if keep_prefix is not None:
                current = {k: v for k, v in current.items() if k.startswith(keep_prefix)}
            current[step_key] = data
            conn.execute(
                text("UPDATE run SET checkpoint = :cp WHERE id = :id"),
                {"cp": json.dumps(current, ensure_ascii=False), "id": run_id},
            )
            conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — never break a run over a checkpoint
        logger.warning("checkpoint.save(%r) failed for run_id=%s: %s", step_key, run_id, exc)
        return False


def clear(run_id: int) -> None:
    """Forget all checkpoints for the run (fresh attempt, or run finished)."""
    if not run_id:
        return
    try:
        with _LOCK, _engine().connect() as conn:
            conn.execute(text("UPDATE run SET checkpoint = NULL WHERE id = :id"), {"id": run_id})
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpoint.clear failed for run_id=%s: %s", run_id, exc)
