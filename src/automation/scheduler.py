"""Cron scheduler: periodically fires runs for Jobs that carry a cron schedule.

Design
------
A single asyncio task (started in ``main.lifespan``) ticks every
``SCHEDULER_INTERVAL`` seconds. On each tick it loads all Jobs with a non-empty
``schedule`` and asks the pure ``_sync_and_collect`` core which are due.

The core keeps an in-memory ``{job_id: next_due}`` map:
- A newly seen job is scheduled for its *next* fire after "now" — so adding a
  schedule never fires immediately (matching cron semantics).
- When ``now >= next_due`` the job is collected and its ``next_due`` advances.
- Jobs whose schedule was removed / that were deleted are dropped from the map.

Because the map is memory-only, a server restart re-anchors every job to its
next future fire (a missed tick during downtime is skipped, not backfilled) —
the same at-most-once-per-interval behaviour cron users expect.
"""
import asyncio
import json
import logging
import os
from datetime import UTC, datetime

from sqlmodel import Session, select

from src.automation.cron_utils import is_valid_cron, next_fire
from src.automation.launcher import launch_run
from src.database import get_engine
from src.models import Job

logger = logging.getLogger(__name__)

SCHEDULER_INTERVAL = int(os.getenv("SCHEDULER_INTERVAL", "30"))  # seconds between ticks


class CronScheduler:
    def __init__(self, interval: int = SCHEDULER_INTERVAL):
        self.interval = max(5, interval)
        self._next_due: dict[int, datetime] = {}
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    # ── pure core (unit-tested without a DB or event loop) ────────────────────
    def _sync_and_collect(self, jobs: list[Job], now: datetime) -> list[Job]:
        """Reconcile the next-due map against ``jobs`` and return the due ones.

        Mutates ``self._next_due``. ``jobs`` is the current set of scheduled jobs.
        """
        seen: set[int] = set()
        due: list[Job] = []
        for job in jobs:
            if job.id is None or not is_valid_cron(job.schedule):
                continue
            seen.add(job.id)
            nxt = self._next_due.get(job.id)
            if nxt is None:
                # First sight → anchor to the next future fire (never fire now).
                self._next_due[job.id] = next_fire(job.schedule, now)
                continue
            if now >= nxt:
                due.append(job)
                # Advance past now so a long tick can't double-fire the same slot.
                self._next_due[job.id] = next_fire(job.schedule, now)
        # Forget jobs that lost their schedule or were deleted.
        for stale in [jid for jid in self._next_due if jid not in seen]:
            self._next_due.pop(stale, None)
        return due

    def next_due_for(self, job_id: int) -> datetime | None:
        return self._next_due.get(job_id)

    # ── async loop ────────────────────────────────────────────────────────────
    def _load_scheduled_jobs(self) -> list[Job]:
        with Session(get_engine()) as s:
            return list(s.exec(select(Job).where(Job.schedule.is_not(None))).all())

    async def tick(self, now: datetime | None = None) -> list[int]:
        """One scheduling pass. Returns the run_ids launched (for tests/logging)."""
        now = now or datetime.now(UTC)
        try:
            jobs = await asyncio.to_thread(self._load_scheduled_jobs)
        except Exception:
            logger.exception("Scheduler: failed to load jobs")
            return []

        launched: list[int] = []
        for job in self._sync_and_collect(jobs, now):
            # Respect the global allowlist — a disabled automation must not run,
            # even on a schedule. Checked lazily to avoid an import cycle.
            from src.settings_store import is_automation_enabled
            if not is_automation_enabled(job.job_type):
                logger.warning("Scheduler: skipping job_id=%d — %s is disabled", job.id, job.job_type)
                continue
            try:
                payload = json.loads(job.payload) if job.payload else {}
            except (json.JSONDecodeError, TypeError):
                logger.error("Scheduler: job_id=%d has unparseable payload; skipping", job.id)
                continue
            try:
                run_id = launch_run(
                    job.id, job.job_type, payload,
                    user_id=getattr(job, "created_by", None), trigger="schedule",
                )
                launched.append(run_id)
                logger.info("Scheduler: fired job_id=%d (%s) → run_id=%d", job.id, job.schedule, run_id)
            except Exception:
                logger.exception("Scheduler: failed to launch job_id=%d", job.id)
        return launched

    async def _run_loop(self) -> None:
        logger.info("Cron scheduler started (interval=%ds)", self.interval)
        while not self._stopped.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval)
            except TimeoutError:
                pass
        logger.info("Cron scheduler stopped")

    def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


# Module-level singleton used by the app lifespan.
scheduler = CronScheduler()
