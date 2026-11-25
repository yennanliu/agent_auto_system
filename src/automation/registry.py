"""In-process task registry for asyncio.Task cancellation."""
import asyncio
import threading

from src.automation import cancellation

_TASKS: dict[int, asyncio.Task] = {}
_LOCK = threading.Lock()


def register(run_id: int, task: asyncio.Task) -> None:
    # A new run may reuse a deleted run's id (SQLite): drop any stale cancel.
    cancellation.clear(run_id)
    with _LOCK:
        _TASKS[run_id] = task


def cancel(run_id: int) -> bool:
    """Cancel the task for run_id. Returns True if the task was found and cancelled."""
    with _LOCK:
        task = _TASKS.pop(run_id, None)
    if task is None or task.done():
        return False
    task.cancel()
    # The flow runs in a worker thread task.cancel() can't reach; this stops it
    # (and closes its browser) at its next log line — see cancellation.py.
    cancellation.request(run_id)
    return True


def unregister(run_id: int) -> None:
    with _LOCK:
        _TASKS.pop(run_id, None)
