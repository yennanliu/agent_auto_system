"""Shared entry point for starting a Run in the background.

Both the HTTP trigger endpoint (``routers/runs.py``) and the cron scheduler
(``scheduler.py``) create a Run row and spawn the executor task through here, so
the create → asyncio task → register sequence lives in exactly one place.
"""
import asyncio
import logging
from datetime import UTC, datetime

from sqlmodel import Session

from src.automation import concurrency
from src.automation.progress import append_log
from src.automation.registry import register, unregister
from src.database import get_engine
from src.models import Run

logger = logging.getLogger(__name__)


def _set_queued(run_id: int, queued: bool) -> None:
    """Flip a run between ``pending`` and ``queued`` (no-op if it already moved on).

    Leaving the queue also resets ``started_at`` so durations measure execution,
    not time spent waiting for a slot.
    """
    src, dst = ("pending", "queued") if queued else ("queued", "pending")
    with Session(get_engine()) as s:
        run = s.get(Run, run_id)
        if run is None or run.status != src:
            return
        run.status = dst
        if not queued:
            run.started_at = datetime.now(UTC)
        s.add(run)
        s.commit()


async def _run_in_background(run_id: int, job_type: str, payload: dict) -> None:
    # Imported lazily to avoid pulling the whole crewai/executor stack at module
    # import time (keeps app startup and test collection fast).
    from src.automation.executor import execute_run
    try:
        run_class = concurrency.run_class(job_type, payload)
        sem = concurrency.semaphore_for(run_class)
        # No await between locked() and acquire(), so this check cannot race.
        queued = sem.locked()
        if queued:
            _set_queued(run_id, True)
            append_log(
                run_id,
                f"Queued — waiting for a free {run_class} slot "
                f"(limit {concurrency.limit_for(run_class)})",
            )
            logger.info("run_id=%d queued (%s slots full)", run_id, run_class)
        # Cancelling a queued run raises CancelledError out of acquire(); the
        # cancel endpoint has already marked the row, and no slot is consumed.
        async with sem:
            if queued:
                _set_queued(run_id, False)
            await execute_run(run_id, job_type, payload)
    finally:
        unregister(run_id)


def launch_run(
    job_id: int,
    job_type: str,
    payload: dict,
    user_id: int | None,
    *,
    trigger: str = "manual",
) -> int:
    """Create a pending Run and spawn its executor task. Returns the new run_id.

    Must be called from within the running event loop (asyncio.create_task).
    The task waits for a per-class concurrency slot (see ``concurrency.py``); a
    run that has to wait shows as ``queued`` until it gets one.
    ``trigger`` is informational ("manual" | "schedule") and recorded in the log.
    """
    with Session(get_engine()) as s:
        run = Run(job_id=job_id, status="pending", user_id=user_id)
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id

    task = asyncio.create_task(_run_in_background(run_id, job_type, payload))
    register(run_id, task)
    logger.info("Launched run_id=%d job_id=%d (%s, trigger=%s)", run_id, job_id, job_type, trigger)
    return run_id
