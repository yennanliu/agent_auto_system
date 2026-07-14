"""Shared entry point for starting a Run in the background.

Both the HTTP trigger endpoint (``routers/runs.py``) and the cron scheduler
(``scheduler.py``) create a Run row and spawn the executor task through here, so
the create → asyncio task → register sequence lives in exactly one place.
"""
import asyncio
import logging

from sqlmodel import Session

from src.automation.registry import register, unregister
from src.database import get_engine
from src.models import Run

logger = logging.getLogger(__name__)


async def _run_in_background(run_id: int, job_type: str, payload: dict) -> None:
    # Imported lazily to avoid pulling the whole crewai/executor stack at module
    # import time (keeps app startup and test collection fast).
    from src.automation.executor import execute_run
    try:
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
