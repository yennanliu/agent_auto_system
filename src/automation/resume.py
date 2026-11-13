"""Resume in-flight runs after a restart instead of failing them all.

On startup every run still marked pending/running is an orphan of the previous
process. :func:`recover_stale_runs` re-launches the ones that

* belong to a job type whose ``AutomationSpec`` declares ``resumable=True``,
* saved at least one checkpoint (``src/automation/checkpoint.py``), and
* have been resumed fewer than ``RUN_RESUME_MAX_ATTEMPTS`` times (default 2) —
  so a run that keeps crashing the process cannot restart forever,

and marks everything else failed exactly as ``reconcile_stale_runs`` always did.
The resumed run keeps its id, log and checkpoint; the flow reads the checkpoint
and skips completed work. Set ``RUN_RESUME_ENABLED=0`` to turn resuming off.
"""
import json
import logging
import os
import re
from dataclasses import dataclass

from sqlalchemy import text

from src.automation import checkpoint, spec
from src.automation.launcher import relaunch_run
from src.automation.progress import append_log

logger = logging.getLogger(__name__)

_STARTED_RE = re.compile(r"^pipeline:(\d+):started$")


def max_resume_attempts() -> int:
    try:
        return max(0, int(os.getenv("RUN_RESUME_MAX_ATTEMPTS", "2")))
    except ValueError:
        return 2


def resume_enabled() -> bool:
    return os.getenv("RUN_RESUME_ENABLED", "1") != "0"


@dataclass(frozen=True)
class ResumePlan:
    run_id: int
    job_type: str
    payload: dict
    attempt: int            # 1-based number of this resume
    checkpoint_keys: tuple[str, ...]


def _refusal(job_type: str, ckpt: dict, resume_count: int, payload: dict) -> str | None:
    """Why a stale run must NOT be resumed, or None if it may be.

    The returned text is shown to the user only for runs that got as far as a
    checkpoint; "" means "fail with the generic restart message".
    """
    if not spec.is_resumable(job_type) or not ckpt:
        return ""
    if resume_count >= max_resume_attempts():
        return (f"Server restarted while run was in progress (not resumed: "
                f"resume limit of {max_resume_attempts()} reached)")
    if job_type == "pipeline":
        # An edited pipeline no longer matches its stored step results; re-running
        # "changed" steps could repeat a send/apply step that already completed.
        if ckpt.get("pipeline:sig") != checkpoint.signature(payload.get("steps", [])):
            return ("Server restarted while run was in progress (not resumed: "
                    "the pipeline was edited after this run started)")
        # A step with `started` but no `done` was in flight. It may have acted
        # already (the crash can fall between the action and its checkpoint), so
        # re-running it is only allowed when that step's own type is resumable.
        for key, data in ckpt.items():
            m = _STARTED_RE.match(key)
            if not m or f"pipeline:{m.group(1)}:done" in ckpt:
                continue
            step_type = data.get("job_type", "") if isinstance(data, dict) else ""
            if not spec.is_resumable(step_type) or step_type == "pipeline":
                return (f"Server restarted while run was in progress (not resumed: "
                        f"step {int(m.group(1)) + 1} '{step_type}' was in flight and "
                        f"is not safe to repeat)")
    return None


def plan_stale_runs() -> tuple[list[ResumePlan], dict[int, str]]:
    """Classify stale runs: (plans to resume, {run_id: specific failure reason})."""
    from src import settings_store
    from src.database import get_engine

    plans: list[ResumePlan] = []
    reasons: dict[int, str] = {}
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT run.id, run.checkpoint, COALESCE(run.resume_count, 0), "
            "       job.job_type, job.payload "
            "FROM run JOIN job ON job.id = run.job_id "
            # 'queued' is not a status on main yet; a sibling change (concurrency
            # caps) adds it as another in-flight state, so it is treated as stale.
            "WHERE run.status IN ('running', 'pending', 'queued') "
            "AND run.checkpoint IS NOT NULL "
            "ORDER BY run.id"
        )).all()

    for run_id, raw_ckpt, resume_count, job_type, raw_payload in rows:
        ckpt = checkpoint.parse(raw_ckpt)
        try:
            payload = json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            continue
        reason = _refusal(job_type, ckpt, resume_count, payload)
        if reason is None and not settings_store.is_automation_enabled(job_type):
            reason = ""
        if reason is not None:
            if reason:
                reasons[run_id] = reason
            continue
        plans.append(ResumePlan(run_id, job_type, payload, resume_count + 1, tuple(sorted(ckpt))))
    return plans, reasons


def recover_stale_runs() -> tuple[list[int], int]:
    """Resume what can be resumed, fail the rest. Returns (resumed ids, failed count).

    Must be called from within the running event loop (it spawns executor tasks).
    Never raises: if planning fails, every stale run is failed as before.
    """
    from src.database import get_engine, reconcile_stale_runs

    plans: list[ResumePlan] = []
    reasons: dict[int, str] = {}
    if resume_enabled():
        try:
            plans, reasons = plan_stale_runs()
        except Exception:  # noqa: BLE001 — startup must not die on resume planning
            logger.exception("Resume planning failed; failing all stale runs instead")
            plans, reasons = [], {}

    failed = reconcile_stale_runs(exclude_ids=[p.run_id for p in plans])

    if reasons:
        with get_engine().connect() as conn:
            for run_id, reason in reasons.items():
                conn.execute(text("UPDATE run SET result = :r WHERE id = :id"),
                             {"r": json.dumps({"error": reason}), "id": run_id})
            conn.commit()

    resumed: list[int] = []
    limit = max_resume_attempts()
    for p in plans:
        # Count the attempt BEFORE launching, so a run that kills the process
        # again still moves towards the cap.
        with get_engine().connect() as conn:
            conn.execute(text("UPDATE run SET status = 'pending', resume_count = :n WHERE id = :id"),
                         {"n": p.attempt, "id": p.run_id})
            conn.commit()
        append_log(p.run_id,
                   f"Resumed after server restart (resume {p.attempt}/{limit}) — "
                   f"continuing from checkpoint: {', '.join(p.checkpoint_keys)}")
        relaunch_run(p.run_id, p.job_type, p.payload)
        resumed.append(p.run_id)
    return resumed, failed
