"""Cooperative cancellation for the worker thread a run executes in.

Cancelling a run (``registry.cancel``) cancels its asyncio task, but the flow
itself runs in a worker thread (``asyncio.to_thread(flow.kickoff)``) that
asyncio cannot interrupt — so a cancelled browser automation kept scanning,
applying and holding its Chromium window open with nobody watching.

``registry.cancel`` therefore also marks the run here, and
``progress.append_log`` — which every flow and tool calls as it works, at least
once per job — calls ``check`` and raises ``RunCancelled`` in the worker thread
on the run's next log line. ``RunCancelled`` is a ``BaseException`` (like
``KeyboardInterrupt``) so the tools' per-job ``except Exception`` handlers
("one bad job never stops the run") do not swallow it; it unwinds through their
``with sync_playwright()`` / ``finally: browser.close()`` blocks, closing the
browser, and through ``ats_common.ledger_claim`` (a claim that never reached
Submit is released, a "pending" one is kept).

The mark is cleared when a run with that id is registered again
(``registry.register``) — SQLite reuses the id of a deleted highest-id run, and
a fresh run must not inherit an old cancel. It is deliberately NOT cleared when
the cancelled task ends: that happens at once, before the worker thread has
reached its next log line.
"""
import threading

_CANCELLED: set[int] = set()
_LOCK = threading.Lock()


class RunCancelled(BaseException):  # noqa: N818 — mirrors asyncio.CancelledError
    """Raised in a cancelled run's worker thread to unwind it."""


def request(run_id: int) -> None:
    """Mark ``run_id`` cancelled; its worker thread stops at its next log line."""
    if run_id:
        with _LOCK:
            _CANCELLED.add(run_id)


def clear(run_id: int) -> None:
    """Forget a cancel for ``run_id`` (a new run with this id is starting)."""
    with _LOCK:
        _CANCELLED.discard(run_id)


def is_cancelled(run_id: int) -> bool:
    with _LOCK:
        return run_id in _CANCELLED


def check(run_id: int) -> None:
    """Raise ``RunCancelled`` if ``run_id`` was cancelled and we are on a worker
    thread. The event-loop (main) thread is never interrupted: the asyncio task
    there is already handled by ``task.cancel()``."""
    if run_id and threading.current_thread() is not threading.main_thread() and is_cancelled(run_id):
        raise RunCancelled(f"run {run_id} was cancelled")
