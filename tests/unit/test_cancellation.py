"""Cancelling a run must stop its worker thread (and close its browser), not
just the asyncio task awaiting it."""
import asyncio
import threading

import pytest

from src.automation import cancellation, progress, registry


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — the test inspects it
            out["exc"] = exc
    t = threading.Thread(target=run)
    t.start()
    t.join(5)
    return out


def test_check_raises_only_on_a_worker_thread_for_a_cancelled_run():
    cancellation.request(90001)
    assert cancellation.is_cancelled(90001) and not cancellation.is_cancelled(90002)
    cancellation.check(90001)                                  # main thread: never raises
    assert isinstance(_in_thread(lambda: cancellation.check(90001))["exc"], cancellation.RunCancelled)
    assert _in_thread(lambda: cancellation.check(90002)) == {"value": None}


def test_run_cancelled_is_not_swallowed_by_except_exception():
    assert not issubclass(cancellation.RunCancelled, Exception)


def test_append_log_stops_a_cancelled_runs_worker_thread(mocker):
    engine = mocker.patch("src.database.get_engine")
    cancellation.request(90003)
    out = _in_thread(lambda: progress.append_log(90003, "scanning job 7"))
    assert isinstance(out["exc"], cancellation.RunCancelled)
    engine.assert_not_called()                                 # nothing written after cancel


async def test_registry_cancel_stops_the_tool_loop_and_closes_its_browser(mocker):
    """End to end: a tool-shaped loop in asyncio.to_thread (per-job
    ``except Exception``, browser closed in ``finally``) stops after cancel."""
    mocker.patch("src.database.get_engine")
    run_id = 90004
    started, stop_seen, closed = threading.Event(), threading.Event(), threading.Event()
    scanned = []

    def tool():
        try:
            for job in range(10_000):
                try:
                    progress.append_log(run_id, f"scan #{job}")
                    scanned.append(job)
                    started.set()
                    threading.Event().wait(0.01)
                except Exception:  # noqa: BLE001 — "one bad job never stops the run"
                    continue
        finally:
            closed.set()                                       # browser.close()
            stop_seen.set()

    task = asyncio.create_task(asyncio.to_thread(tool))
    registry.register(run_id, task)
    await asyncio.to_thread(started.wait, 5)
    assert registry.cancel(run_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(stop_seen.wait, 5), "worker thread kept running after cancel"
    assert closed.is_set()
    assert len(scanned) < 10_000


def test_cancel_of_an_unknown_run_marks_nothing():
    assert not registry.cancel(90005)
    assert not cancellation.is_cancelled(90005)


async def test_a_new_run_reusing_a_cancelled_id_is_not_cancelled():
    """SQLite reuses the id of a deleted highest-id run: a fresh run with that
    id must not inherit the old cancel."""
    cancellation.request(90006)
    task = asyncio.create_task(asyncio.sleep(0))
    registry.register(90006, task)
    assert not cancellation.is_cancelled(90006)
    assert _in_thread(lambda: cancellation.check(90006)) == {"value": None}
    await task
