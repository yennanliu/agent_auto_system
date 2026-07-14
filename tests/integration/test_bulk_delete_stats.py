"""Integration tests for bulk-delete and stats endpoints."""
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from src.models import Job, Run

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_job(session, job_type: str = "web_scraper") -> Job:
    job = Job(
        name=f"Test {job_type}",
        job_type=job_type,
        payload=json.dumps({"url": "https://example.com"}),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _make_run(session, job_id: int, status: str = "success",
              duration_secs: int = 10, *, eval_score=None, eval_confidence=None,
              eval_method=None, eval_notes=None, retry_count=0,
              llm_provider=None, llm_model=None, fallback_used=False,
              validation_passed=None, duration_col=None) -> Run:
    started = datetime.now(UTC)
    finished = started + timedelta(seconds=duration_secs) if status in ("success", "failed") else None
    run = Run(
        job_id=job_id,
        status=status,
        result=json.dumps({"ok": True}) if status == "success" else None,
        started_at=started,
        finished_at=finished,
        eval_score=eval_score,
        eval_confidence=eval_confidence,
        eval_method=eval_method,
        eval_notes=eval_notes,
        retry_count=retry_count,
        llm_provider=llm_provider,
        llm_model=llm_model,
        fallback_used=fallback_used,
        validation_passed=validation_passed,
        duration_secs=duration_col,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


# ── DELETE /api/runs?delete_all=true ─────────────────────────────────────────

async def test_delete_all_removes_completed_runs(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success")
    _make_run(db_session, job.id, "failed")

    resp = await client.delete("/api/runs?delete_all=true")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2

    list_resp = await client.get("/api/runs")
    assert list_resp.json() == []


async def test_delete_all_skips_active_runs(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success")
    active = _make_run(db_session, job.id, "running")

    resp = await client.delete("/api/runs?delete_all=true")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1

    list_resp = await client.get("/api/runs")
    remaining = list_resp.json()
    assert len(remaining) == 1
    assert remaining[0]["id"] == active.id


async def test_delete_all_when_no_runs_returns_zero(client):
    resp = await client.delete("/api/runs?delete_all=true")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0


async def test_delete_all_skips_pending_runs(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "pending")

    resp = await client.delete("/api/runs?delete_all=true")
    assert resp.json()["deleted"] == 0


# ── DELETE /api/runs?ids=… ────────────────────────────────────────────────────

async def test_bulk_delete_by_ids(client, db_session):
    job = _make_job(db_session)
    r1 = _make_run(db_session, job.id, "success")
    r2 = _make_run(db_session, job.id, "success")
    r3 = _make_run(db_session, job.id, "success")

    resp = await client.delete(f"/api/runs?ids={r1.id},{r2.id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2

    list_resp = await client.get("/api/runs")
    remaining_ids = {r["id"] for r in list_resp.json()}
    assert r3.id in remaining_ids
    assert r1.id not in remaining_ids
    assert r2.id not in remaining_ids


async def test_bulk_delete_by_ids_skips_active(client, db_session):
    job = _make_job(db_session)
    r_done = _make_run(db_session, job.id, "success")
    r_active = _make_run(db_session, job.id, "running")

    resp = await client.delete(f"/api/runs?ids={r_done.id},{r_active.id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1

    # Active run must still be there
    get_resp = await client.get(f"/api/runs/{r_active.id}")
    assert get_resp.status_code == 200


async def test_bulk_delete_empty_ids_returns_zero(client):
    resp = await client.delete("/api/runs")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0


async def test_bulk_delete_nonexistent_ids_returns_zero(client):
    resp = await client.delete("/api/runs?ids=9999,8888")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0


# ── GET /api/stats — empty ────────────────────────────────────────────────────

async def test_stats_empty_db_returns_zeros(client):
    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_runs"] == 0
    assert data["success"] == 0
    assert data["failed"] == 0
    assert data["success_rate"] == 0
    assert data["avg_duration_secs"] == 0
    assert data["by_type"] == {}


async def test_stats_trend_has_7_days(client):
    resp = await client.get("/api/stats")
    data = resp.json()
    assert len(data["trend"]) == 7


# ── GET /api/stats — with data ────────────────────────────────────────────────

async def test_stats_total_runs_correct(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success")
    _make_run(db_session, job.id, "success")
    _make_run(db_session, job.id, "failed")

    data = (await client.get("/api/stats")).json()
    assert data["total_runs"] == 3
    assert data["success"] == 2
    assert data["failed"] == 1


async def test_stats_success_rate_calculation(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success")
    _make_run(db_session, job.id, "success")
    _make_run(db_session, job.id, "success")
    _make_run(db_session, job.id, "failed")

    data = (await client.get("/api/stats")).json()
    assert data["success_rate"] == 75.0


async def test_stats_by_type_populated(client, db_session):
    web_job = _make_job(db_session, "web_scraper")
    hn_job = _make_job(db_session, "hacker_news_digest")
    _make_run(db_session, web_job.id, "success")
    _make_run(db_session, web_job.id, "success")
    _make_run(db_session, hn_job.id, "failed")

    data = (await client.get("/api/stats")).json()
    bt = data["by_type"]
    assert bt["web_scraper"]["total"] == 2
    assert bt["web_scraper"]["success"] == 2
    assert bt["hacker_news_digest"]["failed"] == 1


async def test_stats_avg_duration_computed(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success", duration_secs=10)
    _make_run(db_session, job.id, "success", duration_secs=20)

    data = (await client.get("/api/stats")).json()
    assert data["avg_duration_secs"] == 15.0


# ── GET /api/stats — eval trust & reliability (Tier 1) ───────────────────────

async def test_stats_empty_db_has_trust_fields(client):
    data = (await client.get("/api/stats")).json()
    assert data["eval_scored"] == 0
    assert data["eval_llm"] == 0
    assert data["eval_heuristic"] == 0
    assert data["eval_independent_rate"] is None
    assert data["retry_rate"] == 0
    assert data["avg_retries"] == 0


async def test_stats_eval_method_breakdown(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success", eval_score=90, eval_method="llm")
    _make_run(db_session, job.id, "success", eval_score=80, eval_method="llm")
    _make_run(db_session, job.id, "success", eval_score=50, eval_method="heuristic")
    _make_run(db_session, job.id, "success")  # unscored → excluded from breakdown

    data = (await client.get("/api/stats")).json()
    assert data["eval_scored"] == 3
    assert data["eval_llm"] == 2
    assert data["eval_heuristic"] == 1
    assert data["eval_llm_rate"] == round(2 / 3 * 100, 1)


async def test_stats_self_graded_excluded_from_independent(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success", eval_score=90, eval_method="llm",
              eval_notes="great output")
    _make_run(db_session, job.id, "success", eval_score=88, eval_method="llm",
              eval_notes="ok [self-graded: no independent judge available]")

    data = (await client.get("/api/stats")).json()
    assert data["eval_llm"] == 2
    assert data["eval_independent"] == 1
    assert data["eval_independent_rate"] == 50.0


async def test_stats_retry_metrics(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success", retry_count=0)
    _make_run(db_session, job.id, "success", retry_count=2)
    _make_run(db_session, job.id, "failed", retry_count=1)
    _make_run(db_session, job.id, "success", retry_count=0)

    data = (await client.get("/api/stats")).json()
    assert data["retried_runs"] == 2
    assert data["retry_rate"] == 50.0
    assert data["avg_retries"] == round((0 + 2 + 1 + 0) / 4, 2)


async def test_stats_by_type_includes_retried(client, db_session):
    job = _make_job(db_session, "web_scraper")
    _make_run(db_session, job.id, "success", retry_count=1)
    _make_run(db_session, job.id, "success", retry_count=0)

    data = (await client.get("/api/stats")).json()
    assert data["by_type"]["web_scraper"]["retried"] == 1


async def test_stats_by_model_enriched(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success", eval_score=90, eval_confidence=0.8,
              eval_method="llm", retry_count=1,
              llm_provider="anthropic", llm_model="claude-opus-4-8")
    _make_run(db_session, job.id, "success", eval_score=70, eval_confidence=0.6,
              eval_method="heuristic", retry_count=0,
              llm_provider="anthropic", llm_model="claude-opus-4-8")

    data = (await client.get("/api/stats")).json()
    m = data["by_model"]["anthropic"][0]
    assert m["scored"] == 2
    assert m["llm_judged"] == 1
    assert m["retried"] == 1
    assert m["avg_eval_confidence"] == 0.7


# ── GET /api/stats — fallback, validation, duration, trend window (Tier 2/3) ──

async def test_stats_empty_db_has_tier2_fields(client):
    data = (await client.get("/api/stats")).json()
    assert data["fallback_rate"] == 0
    assert data["validation_pass_rate"] is None
    assert data["p50_duration_secs"] is None
    assert data["p95_duration_secs"] is None
    assert data["trend_days"] == 7


async def test_stats_fallback_rate(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success", fallback_used=True)
    _make_run(db_session, job.id, "success", fallback_used=False)
    _make_run(db_session, job.id, "success", fallback_used=False)
    _make_run(db_session, job.id, "success", fallback_used=False)

    data = (await client.get("/api/stats")).json()
    assert data["fallback_runs"] == 1
    assert data["fallback_rate"] == 25.0


async def test_stats_validation_pass_rate(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success", validation_passed=True)
    _make_run(db_session, job.id, "success", validation_passed=True)
    _make_run(db_session, job.id, "success", validation_passed=True)
    _make_run(db_session, job.id, "failed", validation_passed=False)
    _make_run(db_session, job.id, "failed", validation_passed=None)  # hard error → excluded

    data = (await client.get("/api/stats")).json()
    assert data["validated_runs"] == 4
    assert data["validation_fails"] == 1
    assert data["validation_pass_rate"] == 75.0


async def test_stats_duration_percentiles(client, db_session):
    job = _make_job(db_session)
    for secs in [1, 2, 3, 4, 100]:
        _make_run(db_session, job.id, "success", duration_col=float(secs))

    data = (await client.get("/api/stats")).json()
    assert data["p50_duration_secs"] == 3.0        # median of 1,2,3,4,100
    assert data["p95_duration_secs"] == 100.0      # slow tail surfaced


async def test_stats_trend_window_selectable(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success")

    for days, expect in [(7, 7), (14, 14), (30, 30), (99, 7)]:  # 99 → clamp to 7
        data = (await client.get(f"/api/stats?days={days}")).json()
        assert len(data["trend"]) == expect
        assert data["trend_days"] == expect


async def test_stats_trend_has_cost_and_quality(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success", eval_score=90)

    data = (await client.get("/api/stats")).json()
    today = data["trend"][-1]
    assert "cost" in today and "tokens" in today and "avg_score" in today
    assert today["avg_score"] == 90.0


async def test_stats_by_model_cost_efficiency(client, db_session):
    job = _make_job(db_session)
    _make_run(db_session, job.id, "success", eval_score=80,
              llm_provider="anthropic", llm_model="claude-opus-4-8")
    # cost_usd is 0 on hand-made rows → efficiency ratios are 0.0, still present
    data = (await client.get("/api/stats")).json()
    m = data["by_model"]["anthropic"][0]
    assert "cost_per_success" in m and "cost_per_quality" in m and "fallback" in m


async def test_stats_active_count(client, db_session, mocker):
    mocker.patch("src.automation.launcher._run_in_background", new=AsyncMock())
    job = _make_job(db_session)
    _make_run(db_session, job.id, "running")
    _make_run(db_session, job.id, "pending")
    _make_run(db_session, job.id, "success")

    data = (await client.get("/api/stats")).json()
    assert data["active"] == 2
