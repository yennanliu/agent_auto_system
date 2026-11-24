"""Concurrency caps (per-automation queues + global class ceilings) and the ``queued`` status."""
import asyncio

import pytest
from sqlmodel import Session

from src.automation import concurrency, launcher, spec
from src.automation.flow_steps import infer_step_states
from src.models import Job, Run


def test_run_class_follows_spec_browser_flag():
    for job_type, s in spec.REGISTRY.items():
        if job_type == "pipeline":
            continue
        expected = concurrency.BROWSER if s.browser else concurrency.LLM
        assert concurrency.run_class(job_type, {}) == expected
    assert concurrency.run_class("google_form_fill") == concurrency.BROWSER
    assert concurrency.run_class("hacker_news_digest") == concurrency.LLM
    # Custom (no-tools) and unknown types never need a browser slot.
    assert concurrency.run_class("custom:anything") == concurrency.LLM
    assert concurrency.run_class("no_such_type") == concurrency.LLM


def test_pipeline_is_browser_class_when_any_step_is():
    llm_only = {"steps": [{"job_type": "hacker_news_digest"}, {"job_type": "web_scraper"}]}
    mixed = {"steps": [{"job_type": "hacker_news_digest"}, {"job_type": "x_scraper"}]}
    assert concurrency.run_class("pipeline", llm_only) == concurrency.LLM
    assert concurrency.run_class("pipeline", mixed) == concurrency.BROWSER
    assert concurrency.run_class("pipeline", {}) == concurrency.LLM


def test_limits_default_and_env_override(monkeypatch):
    monkeypatch.delenv("MAX_CONCURRENT_BROWSER_RUNS", raising=False)
    monkeypatch.delenv("MAX_CONCURRENT_LLM_RUNS", raising=False)
    assert concurrency.limit_for(concurrency.BROWSER) == 4
    assert concurrency.limit_for(concurrency.LLM) == 8
    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS", "5")
    assert concurrency.limit_for(concurrency.BROWSER) == 5
    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS", "0")       # never 0 → would deadlock
    assert concurrency.limit_for(concurrency.BROWSER) == 1
    monkeypatch.setenv("MAX_CONCURRENT_LLM_RUNS", "not-a-number")
    assert concurrency.limit_for(concurrency.LLM) == 8


def test_type_limits_default_and_env_override(monkeypatch):
    for var in ("MAX_CONCURRENT_BROWSER_RUNS_PER_TYPE", "MAX_CONCURRENT_LLM_RUNS_PER_TYPE",
                "MAX_CONCURRENT_RUNS_LINKEDIN_APPLY", "MAX_CONCURRENT_RUNS_CUSTOM_MY_BOT"):
        monkeypatch.delenv(var, raising=False)
    B, L = concurrency.BROWSER, concurrency.LLM
    assert concurrency.type_limit_for("linkedin_apply", B) == 1
    assert concurrency.type_limit_for("hacker_news_digest", L) == 4

    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS_PER_TYPE", "2")      # class default
    assert concurrency.type_limit_for("linkedin_apply", B) == 2
    assert concurrency.type_limit_for("tw104_apply", B) == 2
    monkeypatch.setenv("MAX_CONCURRENT_RUNS_LINKEDIN_APPLY", "3")        # one type wins
    assert concurrency.type_limit_for("linkedin_apply", B) == 3
    assert concurrency.type_limit_for("tw104_apply", B) == 2
    monkeypatch.setenv("MAX_CONCURRENT_RUNS_LINKEDIN_APPLY", "0")
    assert concurrency.type_limit_for("linkedin_apply", B) == 1

    assert concurrency.type_env_var("custom:my-bot") == "MAX_CONCURRENT_RUNS_CUSTOM_MY_BOT"
    monkeypatch.setenv("MAX_CONCURRENT_RUNS_CUSTOM_MY_BOT", "6")
    assert concurrency.type_limit_for("custom:my-bot", L) == 6


def test_queued_run_shows_all_steps_pending():
    logs = [{"ts": "10:00:00", "msg": "Queued — waiting for a free browser slot (limit 2)"}]
    states = infer_step_states("google_form_fill", logs, "queued")
    assert states and all(s["status"] == "pending" for s in states)


# ── launcher: slots, queueing, cancel ─────────────────────────────────────────

@pytest.fixture
def run_env(test_engine, mocker, monkeypatch):
    """launch_run wired to the in-memory DB with a controllable fake executor."""
    import src.database as _db

    mocker.patch.object(_db, "engine", test_engine)
    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS", "2")
    monkeypatch.setenv("MAX_CONCURRENT_LLM_RUNS", "8")
    # These tests exercise the global ceiling; give each type a roomy queue so
    # only the ceiling binds (per-type behaviour has its own tests below).
    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS_PER_TYPE", "8")
    monkeypatch.setenv("MAX_CONCURRENT_LLM_RUNS_PER_TYPE", "8")

    with Session(test_engine) as s:
        job = Job(name="j", job_type="google_form_fill", payload="{}")
        s.add(job)
        s.commit()
        job_id = job.id

    state = {"running": 0, "peak": 0, "started": [], "release": asyncio.Event()}

    async def fake_execute(run_id, job_type, payload):
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        state["started"].append(run_id)
        with Session(test_engine) as s:
            run = s.get(Run, run_id)
            run.status = "running"
            s.add(run)
            s.commit()
        try:
            await state["release"].wait()
        finally:
            state["running"] -= 1
        with Session(test_engine) as s:
            run = s.get(Run, run_id)
            run.status = "success"
            s.add(run)
            s.commit()

    mocker.patch("src.automation.executor.execute_run", side_effect=fake_execute)

    def status(run_id):
        with Session(test_engine) as s:
            return s.get(Run, run_id).status

    state["job_id"] = job_id
    state["status"] = status
    return state


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_browser_runs_are_capped_and_excess_is_queued(run_env):
    ids = [launcher.launch_run(run_env["job_id"], "google_form_fill", {}, None) for _ in range(4)]
    await _settle()

    assert run_env["started"] == ids[:2]                      # FIFO, only 2 slots
    assert [run_env["status"](i) for i in ids] == ["running", "running", "queued", "queued"]

    run_env["release"].set()
    for _ in range(20):
        await _settle()
        if all(run_env["status"](i) == "success" for i in ids):
            break
    assert [run_env["status"](i) for i in ids] == ["success"] * 4
    assert run_env["peak"] == 2
    assert run_env["started"] == ids


async def test_queued_run_logs_why_it_waits(run_env, test_engine):
    ids = [launcher.launch_run(run_env["job_id"], "google_form_fill", {}, None) for _ in range(3)]
    await _settle()
    with Session(test_engine) as s:
        assert "Queued" in (s.get(Run, ids[2]).log or "")
        assert not s.get(Run, ids[0]).log                     # no noise when a slot is free
    run_env["release"].set()
    await _settle()


async def test_llm_runs_are_not_blocked_by_full_browser_slots(run_env):
    browser = [launcher.launch_run(run_env["job_id"], "google_form_fill", {}, None) for _ in range(3)]
    llm = launcher.launch_run(run_env["job_id"], "hacker_news_digest", {}, None)
    await _settle()
    assert run_env["status"](browser[2]) == "queued"
    assert run_env["status"](llm) == "running"
    run_env["release"].set()
    await _settle()


async def test_cancelling_a_queued_run_frees_no_slot_and_never_executes(run_env):
    from src.automation.registry import cancel

    ids = [launcher.launch_run(run_env["job_id"], "google_form_fill", {}, None) for _ in range(4)]
    await _settle()
    assert run_env["status"](ids[2]) == "queued"

    assert cancel(ids[2]) is True
    await _settle()
    assert run_env["peak"] == 2

    run_env["release"].set()
    for _ in range(20):
        await _settle()
        if run_env["status"](ids[3]) == "success":
            break
    assert ids[2] not in run_env["started"]                   # cancelled while waiting
    assert run_env["started"] == [ids[0], ids[1], ids[3]]     # the next in line still ran


async def test_error_before_execute_marks_run_failed_not_stuck(run_env, test_engine, mocker):
    """An error while classifying/queueing must not leave the row pending forever
    (it would block the job's schedule and be impossible to cancel or delete)."""
    import json

    from src.automation import registry

    mocker.patch.object(concurrency, "run_class", side_effect=RuntimeError("bad spec"))
    run_id = launcher.launch_run(run_env["job_id"], "google_form_fill", {}, None)
    await _settle()

    assert run_env["status"](run_id) == "failed"
    assert run_env["started"] == []                            # never executed
    assert run_id not in registry._TASKS                       # task cleaned up
    with Session(test_engine) as s:
        run = s.get(Run, run_id)
        assert "Failed to start run: bad spec" in json.loads(run.result)["error"]
        assert run.finished_at is not None


# ── per-automation queues ─────────────────────────────────────────────────────

async def _drain(run_env, ids):
    run_env["release"].set()
    for _ in range(40):
        await _settle()
        if all(run_env["status"](i) == "success" for i in ids):
            break


async def test_each_automation_has_its_own_queue(run_env, monkeypatch):
    """A backlog of one automation (e.g. LinkedIn) must not hold up another (104)."""
    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS_PER_TYPE", "1")
    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS", "4")
    li = [launcher.launch_run(run_env["job_id"], "linkedin_apply", {}, None) for _ in range(3)]
    tw = [launcher.launch_run(run_env["job_id"], "tw104_apply", {}, None) for _ in range(2)]
    gh = launcher.launch_run(run_env["job_id"], "greenhouse_apply", {}, None)
    await _settle()

    assert [run_env["status"](i) for i in li] == ["running", "queued", "queued"]
    assert [run_env["status"](i) for i in tw] == ["running", "queued"]
    assert run_env["status"](gh) == "running"
    assert run_env["peak"] == 3

    await _drain(run_env, li + tw + [gh])
    assert all(run_env["status"](i) == "success" for i in li + tw + [gh])
    # FIFO within each queue.
    started = run_env["started"]
    assert [i for i in started if i in li] == li
    assert [i for i in started if i in tw] == tw


async def test_per_type_wait_does_not_hold_a_global_slot(run_env, monkeypatch, test_engine):
    """A run queued behind its own siblings must not occupy a global slot."""
    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS_PER_TYPE", "1")
    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS", "2")
    li = [launcher.launch_run(run_env["job_id"], "linkedin_apply", {}, None) for _ in range(4)]
    tw = launcher.launch_run(run_env["job_id"], "tw104_apply", {}, None)
    await _settle()
    assert run_env["status"](tw) == "running"           # 3 LinkedIn waiters took no global slot
    with Session(test_engine) as s:
        assert "linkedin_apply queue" in (s.get(Run, li[1]).log or "")
    await _drain(run_env, li + [tw])


async def test_global_ceiling_still_applies_across_queues(run_env, monkeypatch, test_engine):
    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS_PER_TYPE", "1")
    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS", "2")
    types = ["linkedin_apply", "tw104_apply", "greenhouse_apply"]
    ids = [launcher.launch_run(run_env["job_id"], t, {}, None) for t in types]
    await _settle()
    assert [run_env["status"](i) for i in ids] == ["running", "running", "queued"]
    with Session(test_engine) as s:
        assert "global limit 2" in (s.get(Run, ids[2]).log or "")
    await _drain(run_env, ids)
    assert run_env["peak"] == 2


async def test_cancel_while_waiting_on_global_releases_its_type_slot(run_env, monkeypatch):
    from src.automation.registry import cancel

    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS_PER_TYPE", "1")
    monkeypatch.setenv("MAX_CONCURRENT_BROWSER_RUNS", "1")
    a = launcher.launch_run(run_env["job_id"], "linkedin_apply", {}, None)
    b1 = launcher.launch_run(run_env["job_id"], "tw104_apply", {}, None)   # holds 104 queue, waits global
    b2 = launcher.launch_run(run_env["job_id"], "tw104_apply", {}, None)   # waits 104 queue
    await _settle()
    assert cancel(b1) is True
    await _settle()
    await _drain(run_env, [a, b2])
    assert run_env["started"] == [a, b2]                # b2 got the 104 slot b1 gave back
