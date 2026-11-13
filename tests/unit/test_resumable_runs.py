"""Resumable runs (E6): checkpoint store, startup recovery, pipeline + email_collect resume."""
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session

from src.automation import checkpoint, spec
from src.models import Job, Run


@pytest.fixture
def db(test_engine, mocker):
    """Point every get_engine() consumer at the in-memory test database."""
    import src.database as _db
    mocker.patch.object(_db, "engine", test_engine)
    mocker.patch("src.automation.executor.get_engine", return_value=test_engine)
    return test_engine


def _job(db, job_type="email_collect", payload=None) -> int:
    with Session(db) as s:
        job = Job(name="j", job_type=job_type, payload=json.dumps(payload or {"query": "cafe"}))
        s.add(job)
        s.commit()
        s.refresh(job)
        return job.id


def _run(db, job_id, status="running", ckpt=None, resume_count=0) -> int:
    with Session(db) as s:
        run = Run(job_id=job_id, status=status, resume_count=resume_count,
                  checkpoint=json.dumps(ckpt) if ckpt is not None else None)
        s.add(run)
        s.commit()
        s.refresh(run)
        return run.id


def _get(db, run_id) -> Run:
    with Session(db) as s:
        return s.get(Run, run_id)


# ── checkpoint store ──────────────────────────────────────────────────────────

def test_checkpoint_round_trip(db):
    rid = _run(db, _job(db))
    assert checkpoint.load(rid) == {}
    assert checkpoint.save(rid, "a", {"n": 1, "name": "臺北"}) is True
    assert checkpoint.save(rid, "b", [1, 2])
    assert checkpoint.load(rid) == {"a": {"n": 1, "name": "臺北"}, "b": [1, 2]}
    checkpoint.save(rid, "a", {"n": 2})  # overwrite one key, keep the other
    assert checkpoint.load(rid) == {"a": {"n": 2}, "b": [1, 2]}
    checkpoint.clear(rid)
    assert checkpoint.load(rid) == {}


def test_checkpoint_keep_prefix_drops_other_keys(db):
    rid = _run(db, _job(db))
    checkpoint.save(rid, "pipeline:0:started", {"job_type": "email_collect"})
    checkpoint.save(rid, "email_collect:crawl", {"done": 3})
    checkpoint.save(rid, "pipeline:0:done", {"result": {}}, keep_prefix="pipeline:")
    assert set(checkpoint.load(rid)) == {"pipeline:0:started", "pipeline:0:done"}


def test_checkpoint_is_best_effort(db):
    # No run row / run_id 0 (unit-test flows) / unserialisable data: never raises.
    assert checkpoint.save(0, "a", 1) is False
    assert checkpoint.save(9999, "a", 1) is False
    assert checkpoint.load(0) == {} and checkpoint.load(9999) == {}
    rid = _run(db, _job(db))
    assert checkpoint.save(rid, "a", {1, 2}) is False
    assert checkpoint.parse("not json") == {} and checkpoint.parse("[1]") == {}


def test_signature_is_stable_and_input_sensitive():
    assert checkpoint.signature({"a": 1, "b": 2}) == checkpoint.signature({"b": 2, "a": 1})
    assert checkpoint.signature({"a": 1}) != checkpoint.signature({"a": 2})


def test_terminal_status_clears_checkpoint(db):
    from src.automation.executor import _update_run
    rid = _run(db, _job(db), ckpt={"a": 1})
    _update_run(rid, "running")
    assert _get(db, rid).checkpoint is not None
    _update_run(rid, "success", {"ok": True})
    assert _get(db, rid).checkpoint is None


# ── spec declaration ──────────────────────────────────────────────────────────

def test_only_side_effect_free_types_are_resumable():
    assert spec.is_resumable("email_collect") and spec.is_resumable("pipeline")
    # Outward side effects: a resume could repeat an application / a sent email.
    for jt in ("tasker_apply", "tw104_apply", "linkedin_apply", "greenhouse_apply",
               "email_sender", "google_form_fill"):
        assert not spec.is_resumable(jt), jt
    assert not spec.is_resumable("no_such_type")
    assert {jt for jt in spec.REGISTRY if spec.is_resumable(jt)} == {"email_collect", "pipeline"}


# ── startup recovery ──────────────────────────────────────────────────────────

@pytest.fixture
def relaunch(mocker):
    return mocker.patch("src.automation.resume.relaunch_run")


def test_recover_resumes_checkpointed_resumable_and_fails_the_rest(db, relaunch):
    from src.automation.resume import recover_stale_runs
    ec = _job(db, "email_collect", {"query": "cafe", "limit": 5})
    tasker = _job(db, "tasker_apply", {"keywords": "x"})

    resumable = _run(db, ec, "running", ckpt={"email_collect:discovery": {"businesses": []}})
    no_ckpt = _run(db, ec, "running")
    pending_no_ckpt = _run(db, ec, "pending")
    side_effect = _run(db, tasker, "running", ckpt={"applied": ["case-1"]})
    finished = _run(db, ec, "success", ckpt=None)

    resumed, failed = recover_stale_runs()

    assert resumed == [resumable] and failed == 3
    relaunch.assert_called_once_with(resumable, "email_collect", {"query": "cafe", "limit": 5})
    r = _get(db, resumable)
    assert r.status == "pending" and r.resume_count == 1 and r.checkpoint is not None
    assert "Resumed after server restart (resume 1/2)" in r.log
    assert "email_collect:discovery" in r.log
    for rid in (no_ckpt, pending_no_ckpt, side_effect):
        f = _get(db, rid)
        assert f.status == "failed" and "Server restarted" in f.result
        assert f.resume_count == 0
    assert _get(db, finished).status == "success"


def test_recover_caps_resume_attempts(db, relaunch):
    from src.automation.resume import recover_stale_runs
    ec = _job(db)
    ckpt = {"email_collect:discovery": {"businesses": []}}
    second = _run(db, ec, "running", ckpt=ckpt, resume_count=1)
    exhausted = _run(db, ec, "running", ckpt=ckpt, resume_count=2)

    resumed, failed = recover_stale_runs()

    assert resumed == [second] and failed == 1
    assert _get(db, second).resume_count == 2
    x = _get(db, exhausted)
    assert x.status == "failed" and "resume limit of 2 reached" in x.result

    # Crash loop: the same run comes back stale again → now over the cap too.
    with Session(db) as s:
        run = s.get(Run, second)
        run.status = "running"
        s.add(run)
        s.commit()
    relaunch.reset_mock()
    assert recover_stale_runs() == ([], 1)
    relaunch.assert_not_called()
    assert _get(db, second).status == "failed"


def test_recover_disabled_by_env_fails_everything(db, relaunch, monkeypatch):
    from src.automation.resume import recover_stale_runs
    monkeypatch.setenv("RUN_RESUME_ENABLED", "0")
    rid = _run(db, _job(db), "running", ckpt={"email_collect:discovery": {}})
    assert recover_stale_runs() == ([], 1)
    relaunch.assert_not_called()
    assert _get(db, rid).status == "failed"


def test_recover_skips_disabled_automation(db, relaunch, mocker):
    from src.automation.resume import recover_stale_runs
    mocker.patch("src.settings_store.is_automation_enabled", return_value=False)
    rid = _run(db, _job(db), "running", ckpt={"email_collect:discovery": {}})
    assert recover_stale_runs() == ([], 1)
    assert _get(db, rid).status == "failed"


def test_recover_falls_back_to_failing_all_when_planning_breaks(db, relaunch, mocker):
    from src.automation.resume import recover_stale_runs
    mocker.patch("src.automation.resume.plan_stale_runs", side_effect=RuntimeError("boom"))
    rid = _run(db, _job(db), "running", ckpt={"email_collect:discovery": {}})
    assert recover_stale_runs() == ([], 1)
    assert _get(db, rid).status == "failed"


_PIPE = [
    {"job_type": "email_collect", "payload": {"query": "cafe"}},
    {"job_type": "email_sender", "payload": {"to": "{{steps.0.result.leads[].email}}"}},
]


def test_recover_pipeline_resumes_when_in_flight_step_is_resumable(db, relaunch):
    from src.automation.resume import recover_stale_runs
    job = _job(db, "pipeline", {"steps": _PIPE})
    rid = _run(db, job, "running", ckpt={
        "pipeline:sig": checkpoint.signature(_PIPE),
        "pipeline:0:started": {"job_type": "email_collect"},
    })
    assert recover_stale_runs() == ([rid], 0)


def test_recover_pipeline_refuses_in_flight_side_effect_step(db, relaunch):
    """Crash while email_sender was running: mail may be out, never send again."""
    from src.automation.resume import recover_stale_runs
    job = _job(db, "pipeline", {"steps": _PIPE})
    rid = _run(db, job, "running", ckpt={
        "pipeline:sig": checkpoint.signature(_PIPE),
        "pipeline:0:started": {"job_type": "email_collect"},
        "pipeline:0:done": {"sig": "x", "result": {}},
        "pipeline:1:started": {"job_type": "email_sender"},
    })
    assert recover_stale_runs() == ([], 1)
    relaunch.assert_not_called()
    r = _get(db, rid)
    assert r.status == "failed"
    assert "step 2 'email_sender' was in flight and is not safe to repeat" in r.result


def test_recover_pipeline_refuses_edited_job(db, relaunch):
    from src.automation.resume import recover_stale_runs
    job = _job(db, "pipeline", {"steps": _PIPE})
    rid = _run(db, job, "running", ckpt={
        "pipeline:sig": checkpoint.signature([{"job_type": "email_sender", "payload": {}}]),
        "pipeline:0:started": {"job_type": "email_collect"},
    })
    assert recover_stale_runs() == ([], 1)
    assert "edited" in _get(db, rid).result


def test_reconcile_stale_runs_default_still_fails_all(db):
    from src.database import reconcile_stale_runs
    job = _job(db)
    a, b = _run(db, job, "running", ckpt={"k": 1}), _run(db, job, "pending")
    assert reconcile_stale_runs() == 2
    assert _get(db, a).status == "failed" and _get(db, b).status == "failed"


@pytest.mark.asyncio
async def test_relaunch_run_reuses_the_run_row(db, mocker):
    from src.automation import registry
    from src.automation.launcher import relaunch_run
    executed = mocker.patch("src.automation.executor.execute_run", new=AsyncMock())
    rid = _run(db, _job(db), "pending", ckpt={"k": 1})
    relaunch_run(rid, "email_collect", {"query": "cafe"})
    task = registry._TASKS[rid]
    await task
    executed.assert_awaited_once_with(rid, "email_collect", {"query": "cafe"})
    assert rid not in registry._TASKS
    with Session(db) as s:
        assert len(s.exec(Run.__table__.select()).all()) == 1  # no new Run created


# ── pipeline ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pipeline_resume_skips_completed_steps(db):
    from src.automation.pipeline import execute_pipeline
    steps = [
        {"job_type": "web_scraper", "payload": {"url": "https://a.com"}},
        {"job_type": "hacker_news_digest", "payload": {"note": "{{steps.0.result.summary}}"}},
    ]
    rid = _run(db, _job(db, "pipeline", {"steps": steps}))
    usage = {"prompt_tokens": 10, "completion_tokens": 5}

    # First process: step 1 completes, the "restart" hits during step 2.
    first = AsyncMock(side_effect=[({"summary": "S1"}, usage, {}), RuntimeError("process died")])
    with patch("src.automation.executor._run_flow", new=first):
        with pytest.raises(RuntimeError):
            await execute_pipeline(rid, steps, "gemini", "m")
    saved = checkpoint.load(rid)
    assert saved["pipeline:0:done"]["result"] == {"summary": "S1"}
    assert "pipeline:1:started" in saved and "pipeline:1:done" not in saved

    # Resumed process: only step 2 runs, fed step 1's stored result.
    second = AsyncMock(return_value=({"digest": "D"}, usage, {}))
    with patch("src.automation.executor._run_flow", new=second):
        result, total = await execute_pipeline(rid, steps, "gemini", "m")

    assert second.await_count == 1
    assert second.await_args.args[1] == "hacker_news_digest"
    assert second.await_args.args[2] == {"note": "S1"}
    assert [s["result"] for s in result["steps"]] == [{"summary": "S1"}, {"digest": "D"}]
    assert total == {"prompt_tokens": 20, "completion_tokens": 10}  # skipped step's usage kept
    assert "Skipped web_scraper" in _get(db, rid).log


@pytest.mark.asyncio
async def test_pipeline_reruns_step_whose_payload_changed(db):
    from src.automation.pipeline import execute_pipeline
    steps = [{"job_type": "web_scraper", "payload": {"url": "https://a.com"}}]
    rid = _run(db, _job(db, "pipeline", {"steps": steps}))
    flow = AsyncMock(return_value=({"summary": "S"}, {}, {}))
    with patch("src.automation.executor._run_flow", new=flow):
        await execute_pipeline(rid, steps, "gemini", "m")
        edited = [{"job_type": "web_scraper", "payload": {"url": "https://b.com"}}]
        await execute_pipeline(rid, edited, "gemini", "m")
    assert flow.await_count == 2


@pytest.mark.asyncio
async def test_pipeline_step_completion_drops_nested_flow_checkpoints(db):
    from src.automation.pipeline import execute_pipeline
    steps = [{"job_type": "email_collect", "payload": {"query": "cafe"}}]
    rid = _run(db, _job(db, "pipeline", {"steps": steps}))

    async def fake_flow(run_id, *_a):
        checkpoint.save(run_id, "email_collect:crawl", {"done": 1})
        return {"leads": []}, {}, {}

    with patch("src.automation.executor._run_flow", new=fake_flow):
        await execute_pipeline(rid, steps, "gemini", "m")
    assert all(k.startswith("pipeline:") for k in checkpoint.load(rid))


# ── email_collect ─────────────────────────────────────────────────────────────

def _biz(name):
    return {"name": name, "website": f"https://{name}.com", "category": "cafe",
            "phone": "", "address": "", "maps_url": ""}


def _patch_email_collect(mocker, crash_on=None):
    lc = "src.automation.flows.email_collect_flow"
    maps = mocker.patch(f"{lc}.search_maps", return_value={
        "businesses": [_biz("a"), _biz("b"), _biz("c")], "warnings": ["partial page"]})

    def _extract(site, log=None, render=False):
        if site == crash_on:
            raise RuntimeError("process died")
        return {"emails": [f"info@{site.split('//')[1]}"], "guessed": False}

    extract = mocker.patch(f"{lc}.extract_emails", side_effect=_extract)
    verify = mocker.patch(f"{lc}.verify_email", side_effect=lambda email, smtp_check=True: {
        "email": email, "confidence": "medium", "mx_found": True, "smtp_status": "unknown"})
    mocker.patch("src.automation.harness.provider.resolve", side_effect=RuntimeError("no key"))
    return maps, extract, verify


def test_email_collect_resumes_without_redoing_completed_work(db, mocker):
    from src.automation.flows.email_collect_flow import EmailCollectFlow
    rid = _run(db, _job(db))
    inputs = {"query": "cafe", "region": "TW", "run_id": rid}

    # First process dies while crawling the 2nd of 3 businesses.
    maps, extract, _ = _patch_email_collect(mocker, crash_on="https://b.com")
    with pytest.raises(Exception):
        EmailCollectFlow().kickoff(inputs=dict(inputs))
    saved = checkpoint.load(rid)
    assert len(saved["email_collect:discovery"]["businesses"]) == 3
    assert saved["email_collect:crawl"]["done"] == 1
    assert [x["email"] for x in saved["email_collect:crawl"]["leads"]] == ["info@a.com"]

    # Resumed process: discovery is not repeated, business A is not re-crawled.
    maps, extract, verify = _patch_email_collect(mocker)
    raw = EmailCollectFlow().kickoff(inputs=dict(inputs))
    d = json.loads(raw.raw if hasattr(raw, "raw") else str(raw))

    maps.assert_not_called()
    assert [c.args[0] for c in extract.call_args_list] == ["https://b.com", "https://c.com"]
    assert [c.args[0] for c in verify.call_args_list] == ["info@b.com", "info@c.com"]
    assert d["discovered_count"] == 3 and d["with_website"] == 3
    assert sorted(x["email"] for x in d["leads"]) == ["info@a.com", "info@b.com", "info@c.com"]
    assert d["warnings"] == ["partial page"]  # discovery warnings survive the resume
    log = _get(db, rid).log
    assert "reusing 3 business(es)" in log and "1/3 business(es) already crawled" in log


def test_email_collect_ignores_checkpoint_from_different_inputs(db, mocker):
    from src.automation.flows.email_collect_flow import EmailCollectFlow
    rid = _run(db, _job(db))
    _patch_email_collect(mocker)
    EmailCollectFlow().kickoff(inputs={"query": "cafe", "run_id": rid})

    maps, extract, _ = _patch_email_collect(mocker)
    EmailCollectFlow().kickoff(inputs={"query": "bakery", "run_id": rid})  # job edited
    maps.assert_called_once()
    assert extract.call_count == 3


def test_executor_retry_starts_from_a_clean_checkpoint(db, mocker):
    """An in-process retry is a fresh attempt — it must not replay the rejected one."""
    import asyncio

    from src.automation.executor import execute_run
    rid = _run(db, _job(db), "pending")
    seen: list[dict] = []

    async def fake_flow(run_id, *_a):
        seen.append(checkpoint.load(run_id))
        checkpoint.save(run_id, "email_collect:discovery", {"businesses": []})
        return {"discovered_count": 0 if len(seen) == 1 else 2,
                "note": "long enough to satisfy the validator's minimum content length"}, {}, {
            "served_model": "m", "models_attempted": 1, "fallback_used": False}

    mocker.patch("src.automation.executor._run_flow", new=fake_flow)
    asyncio.run(execute_run(rid, "email_collect", {"query": "cafe", "max_retries": 1}))
    assert seen == [{}, {}]
    r = _get(db, rid)
    assert r.status == "success" and r.checkpoint is None
