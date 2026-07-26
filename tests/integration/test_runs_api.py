import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from src.models import Job, Run


async def test_trigger_run_returns_202(client, seed_job, mocker):
    mocker.patch("src.automation.launcher._run_in_background", new=AsyncMock())
    resp = await client.post(f"/api/jobs/{seed_job.id}/run")
    assert resp.status_code == 202
    data = resp.json()
    assert "run_id" in data
    assert data["status"] == "pending"


async def test_trigger_run_job_not_found(client):
    resp = await client.post("/api/jobs/9999/run")
    assert resp.status_code == 404


async def test_list_runs_empty(client):
    resp = await client.get("/api/runs")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_run_not_found(client):
    resp = await client.get("/api/runs/9999")
    assert resp.status_code == 404


async def test_get_run_after_trigger(client, seed_job, mocker):
    mocker.patch("src.automation.launcher._run_in_background", new=AsyncMock())
    trig = await client.post(f"/api/jobs/{seed_job.id}/run")
    run_id = trig.json()["run_id"]

    resp = await client.get(f"/api/runs/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


async def test_list_runs_after_trigger(client, seed_job, mocker):
    mocker.patch("src.automation.launcher._run_in_background", new=AsyncMock())
    await client.post(f"/api/jobs/{seed_job.id}/run")
    await client.post(f"/api/jobs/{seed_job.id}/run")

    resp = await client.get("/api/runs")
    assert len(resp.json()) == 2


async def test_run_transitions_to_success(client, seed_job, mocker):
    async def immediate_success(run_id, job_type, payload):
        from src.automation.executor import _update_run
        _update_run(run_id, "success", {"digest": "Top stories today"})

    mocker.patch("src.automation.launcher._run_in_background", side_effect=immediate_success)
    trig = await client.post(f"/api/jobs/{seed_job.id}/run")
    assert trig.status_code == 202
    run_id = trig.json()["run_id"]

    import asyncio
    await asyncio.sleep(0.1)

    resp = await client.get(f"/api/runs/{run_id}")
    assert resp.json()["status"] == "success"


async def test_run_transitions_to_failed(client, seed_job, mocker):
    async def immediate_failure(run_id, job_type, payload):
        from src.automation.executor import _update_run
        _update_run(run_id, "failed", {"error": "something broke"})

    mocker.patch("src.automation.launcher._run_in_background", side_effect=immediate_failure)
    trig = await client.post(f"/api/jobs/{seed_job.id}/run")
    run_id = trig.json()["run_id"]

    import asyncio
    await asyncio.sleep(0.1)

    resp = await client.get(f"/api/runs/{run_id}")
    assert resp.json()["status"] == "failed"


# ── Abort / cancel a run ─────────────────────────────────────────────────────────

async def test_cancel_run_marks_failed(client, db_session, seed_job, mocker):
    run = Run(job_id=seed_job.id, status="running")
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    # Registry has a live task for this run → cancel succeeds.
    mocker.patch("src.routers.runs.cancel_task", return_value=True)

    resp = await client.post(f"/api/runs/{run.id}/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True, "run_id": run.id}

    db_session.refresh(run)
    assert run.status == "failed"
    assert json.loads(run.result)["error"] == "Cancelled by user"
    assert run.finished_at is not None


async def test_cancel_run_no_live_task(client, db_session, seed_job, mocker):
    # Run row says running but the registry has no task (e.g. after restart);
    # the endpoint reports cancelled=False and leaves the row untouched.
    run = Run(job_id=seed_job.id, status="running")
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    orig_result, orig_finished = run.result, run.finished_at
    mocker.patch("src.routers.runs.cancel_task", return_value=False)

    resp = await client.post(f"/api/runs/{run.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is False
    db_session.refresh(run)
    # No live task → the row is left entirely as-is, not just its status.
    assert run.status == "running"
    assert run.result == orig_result
    assert run.finished_at == orig_finished


async def test_cancel_run_not_active_conflict(client, db_session, seed_job):
    run = Run(job_id=seed_job.id, status="success", result=json.dumps({"ok": True}))
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    orig_result, orig_finished = run.result, run.finished_at
    resp = await client.post(f"/api/runs/{run.id}/cancel")
    assert resp.status_code == 409
    # 409 rejects before any mutation — result/finished_at untouched.
    db_session.refresh(run)
    assert run.status == "success"
    assert run.result == orig_result
    assert run.finished_at == orig_finished


async def test_cancel_run_not_found(client):
    resp = await client.post("/api/runs/9999/cancel")
    assert resp.status_code == 404


async def test_sse_stream_returns_event_stream(client, db_session, seed_job):
    # Seed a completed run so the generator exits immediately
    run = Run(job_id=seed_job.id, status="success", result=json.dumps({"submitted": True}))
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    resp = await client.get(f"/api/runs/{run.id}/stream")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert b'"status": "success"' in resp.content


# ── email_collect CSV export ─────────────────────────────────────────────────────

def _seed_lead_run(db_session, result: dict):
    job = Job(name="Leads", job_type="email_collect", payload=json.dumps({"query": "cafe"}))
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    run = Run(job_id=job.id, status="success", result=json.dumps(result))
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _seed_runs_for_filtering(db_session):
    """Two job types, mixed statuses, and spread-out start dates."""
    form_job = Job(name="Form", job_type="google_form_fill", payload="{}")
    web_job = Job(name="Web", job_type="web_scraper", payload="{}")
    db_session.add_all([form_job, web_job])
    db_session.commit()
    db_session.refresh(form_job)
    db_session.refresh(web_job)

    runs = [
        Run(job_id=form_job.id, status="success",
            started_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC)),
        Run(job_id=form_job.id, status="failed",
            started_at=datetime(2026, 7, 5, 10, 0, tzinfo=UTC)),
        Run(job_id=web_job.id, status="success",
            started_at=datetime(2026, 7, 9, 10, 0, tzinfo=UTC)),
    ]
    db_session.add_all(runs)
    db_session.commit()
    return form_job, web_job


async def test_list_runs_filter_by_job_type(client, db_session):
    form_job, _ = _seed_runs_for_filtering(db_session)
    resp = await client.get("/api/runs?job_type=google_form_fill")
    data = resp.json()
    assert len(data) == 2
    assert all(r["job_type"] == "google_form_fill" for r in data)


async def test_list_runs_filter_by_status(client, db_session):
    _seed_runs_for_filtering(db_session)
    resp = await client.get("/api/runs?status=failed")
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == "failed"


async def test_list_runs_filter_by_date_range(client, db_session):
    _seed_runs_for_filtering(db_session)
    # started_after is inclusive; started_before includes the whole named day.
    resp = await client.get("/api/runs?started_after=2026-07-05&started_before=2026-07-09")
    data = resp.json()
    assert len(data) == 2
    assert {r["status"] for r in data} == {"failed", "success"}


async def test_list_runs_filters_combine(client, db_session):
    _seed_runs_for_filtering(db_session)
    resp = await client.get("/api/runs?job_type=google_form_fill&status=success")
    data = resp.json()
    assert len(data) == 1
    assert data[0]["job_type"] == "google_form_fill"
    assert data[0]["status"] == "success"


async def test_list_runs_bad_date_ignored(client, db_session):
    _seed_runs_for_filtering(db_session)
    resp = await client.get("/api/runs?started_after=not-a-date")
    assert resp.status_code == 200
    assert len(resp.json()) == 3  # unparseable filter is silently dropped


async def test_list_runs_filter_accepts_full_datetime(client, db_session):
    _seed_runs_for_filtering(db_session)
    # An explicit timestamp (with time component) narrows within a single day.
    resp = await client.get("/api/runs?started_after=2026-07-05T11:00:00")
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == "success"  # only the 2026-07-09 run is after this


async def test_list_runs_total_count_header(client, db_session):
    _seed_runs_for_filtering(db_session)
    resp = await client.get("/api/runs")
    assert resp.status_code == 200
    assert resp.headers["X-Total-Count"] == "3"  # total ignores the page window


async def test_list_runs_total_count_reflects_filters(client, db_session):
    _seed_runs_for_filtering(db_session)
    resp = await client.get("/api/runs?job_type=google_form_fill")
    assert resp.headers["X-Total-Count"] == "2"  # count is filter-aware


async def test_list_runs_pagination_window(client, db_session):
    _seed_runs_for_filtering(db_session)
    # A page smaller than the total returns just the window but the full count.
    resp = await client.get("/api/runs?limit=2&offset=0")
    assert len(resp.json()) == 2
    assert resp.headers["X-Total-Count"] == "3"
    # The last page returns the remainder.
    resp2 = await client.get("/api/runs?limit=2&offset=2")
    assert len(resp2.json()) == 1
    assert resp2.headers["X-Total-Count"] == "3"


async def test_leads_csv_export(client, db_session):
    run = _seed_lead_run(db_session, {
        "discovered_count": 2, "with_website": 1, "lead_count": 1,
        "leads": [{
            "company": "隱寓咖啡", "email": "info@acme.com", "confidence": "medium",
            "icp_fit": 4, "hook": "Automate your bookings", "website": "https://acme.com",
        }],
    })
    resp = await client.get(f"/api/runs/{run.id}/leads.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.content.decode("utf-8-sig")  # strip BOM
    lines = body.strip().splitlines()
    assert lines[0].startswith("company,email,confidence,icp_fit,hook")
    assert "info@acme.com" in body
    assert "隱寓咖啡" in body  # UTF-8 round-trips


async def test_leads_csv_rejects_non_lead_job(client, db_session, seed_job):
    run = Run(job_id=seed_job.id, status="success", result=json.dumps({"submitted": True}))
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    resp = await client.get(f"/api/runs/{run.id}/leads.csv")
    assert resp.status_code == 400


async def test_leads_csv_run_not_found(client):
    resp = await client.get("/api/runs/9999/leads.csv")
    assert resp.status_code == 404


async def test_leads_csv_empty_leads(client, db_session):
    run = _seed_lead_run(db_session, {"discovered_count": 0, "lead_count": 0, "leads": []})
    resp = await client.get(f"/api/runs/{run.id}/leads.csv")
    assert resp.status_code == 200
    # Header row only, no data rows.
    assert len(resp.content.decode("utf-8-sig").strip().splitlines()) == 1
