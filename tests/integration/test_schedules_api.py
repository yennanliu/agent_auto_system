import json
from datetime import UTC, datetime

from src.models import Run

FORM_PAYLOAD = {
    "name": "Daily Form",
    "job_type": "google_form_fill",
    "payload": {"company_name": "Acme", "company_size": "0-10", "ai_problem": "triage"},
}


# ── create / validation ───────────────────────────────────────────────────────

async def test_create_job_with_valid_schedule(client):
    resp = await client.post("/api/jobs", json={**FORM_PAYLOAD, "schedule": "0 8 * * *"})
    assert resp.status_code == 201
    assert resp.json()["schedule"] == "0 8 * * *"


async def test_create_job_expands_macro_schedule(client):
    resp = await client.post("/api/jobs", json={**FORM_PAYLOAD, "schedule": "@daily"})
    assert resp.status_code == 201
    assert resp.json()["schedule"] == "0 0 * * *"


async def test_create_job_with_invalid_schedule_400(client):
    resp = await client.post("/api/jobs", json={**FORM_PAYLOAD, "schedule": "not a cron"})
    assert resp.status_code == 400


async def test_create_job_records_owner(client, seed_admin):
    resp = await client.post("/api/jobs", json=FORM_PAYLOAD)
    assert resp.json()["created_by"] == seed_admin.id


# ── PATCH update ──────────────────────────────────────────────────────────────

async def test_patch_sets_schedule(client):
    job = (await client.post("/api/jobs", json=FORM_PAYLOAD)).json()
    resp = await client.patch(f"/api/jobs/{job['id']}", json={"schedule": "*/15 * * * *"})
    assert resp.status_code == 200
    assert resp.json()["schedule"] == "*/15 * * * *"


async def test_patch_invalid_schedule_400(client):
    job = (await client.post("/api/jobs", json=FORM_PAYLOAD)).json()
    resp = await client.patch(f"/api/jobs/{job['id']}", json={"schedule": "99 99 * * *"})
    assert resp.status_code == 400


async def test_patch_clears_schedule(client):
    job = (await client.post("/api/jobs", json={**FORM_PAYLOAD, "schedule": "@daily"})).json()
    resp = await client.patch(f"/api/jobs/{job['id']}", json={"schedule": None})
    assert resp.status_code == 200
    assert resp.json()["schedule"] is None


async def test_patch_updates_name_only_leaves_schedule(client):
    job = (await client.post("/api/jobs", json={**FORM_PAYLOAD, "schedule": "@daily"})).json()
    resp = await client.patch(f"/api/jobs/{job['id']}", json={"name": "Renamed"})
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["schedule"] == "0 0 * * *"  # unchanged


async def test_patch_missing_job_404(client):
    resp = await client.patch("/api/jobs/9999", json={"name": "x"})
    assert resp.status_code == 404


# ── GET /schedules ────────────────────────────────────────────────────────────

async def test_list_schedules_empty(client):
    await client.post("/api/jobs", json=FORM_PAYLOAD)  # no schedule → excluded
    resp = await client.get("/api/schedules")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_schedules_returns_next_run_and_last_run(client, db_session):
    job = (await client.post("/api/jobs", json={**FORM_PAYLOAD, "schedule": "0 8 * * *"})).json()
    # Two runs → run_count aggregates both; last_run is the latest (highest id).
    db_session.add(Run(job_id=job["id"], status="success",
                       started_at=datetime(2026, 1, 1, tzinfo=UTC)))
    db_session.add(Run(job_id=job["id"], status="failed",
                       started_at=datetime(2026, 1, 2, tzinfo=UTC)))
    db_session.commit()

    rows = (await client.get("/api/schedules")).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["job_id"] == job["id"]
    assert row["schedule"] == "0 8 * * *"
    assert row["valid"] is True
    assert row["next_run_at"] is not None
    assert row["run_count"] == 2
    assert row["last_run"]["status"] == "failed"  # most recent (max id)


# ── GET /jobs/{id}/overview ───────────────────────────────────────────────────

async def test_overview_missing_job_404(client):
    resp = await client.get("/api/jobs/9999/overview")
    assert resp.status_code == 404


async def test_overview_returns_task_grid(client, db_session):
    job = (await client.post("/api/jobs", json=FORM_PAYLOAD)).json()
    log = json.dumps([
        {"ts": "00:00:01", "msg": "Starting google_form_fill..."},
        {"ts": "00:00:02", "msg": "Payload validated"},
        {"ts": "00:00:03", "msg": "Inspecting Google Form"},
    ])
    db_session.add(Run(job_id=job["id"], status="failed", log=log,
                       started_at=datetime(2026, 1, 1, tzinfo=UTC)))
    db_session.commit()

    data = (await client.get(f"/api/jobs/{job['id']}/overview")).json()
    assert data["job"]["id"] == job["id"]
    assert data["task_names"][0] == "Start"
    assert len(data["runs"]) == 1
    steps = {s["name"]: s["status"] for s in data["runs"][0]["steps"]}
    assert steps["Start"] == "done"
    assert steps["Validate"] == "done"
    assert steps["Inspect Form"] == "failed"  # furthest reached + run failed
    assert steps["Submit"] == "pending"


# ── GET /overview (index, grouped by automation type) ─────────────────────────

async def test_overview_index_groups_by_type(client, db_session):
    # Two jobs of the SAME type, each with a run → one grouped entry, count 2.
    j1 = (await client.post("/api/jobs", json=FORM_PAYLOAD)).json()
    j2 = (await client.post("/api/jobs", json={**FORM_PAYLOAD, "name": "Form B"})).json()
    for jid in (j1["id"], j2["id"]):
        db_session.add(Run(job_id=jid, status="success",
                           started_at=datetime(2026, 1, 1, tzinfo=UTC)))
    db_session.commit()

    rows = (await client.get("/api/overview")).json()
    entry = next(r for r in rows if r["job_type"] == "google_form_fill")
    assert entry["run_count"] == 2


async def test_overview_index_excludes_types_without_runs(client):
    await client.post("/api/jobs", json=FORM_PAYLOAD)  # job but no runs
    assert (await client.get("/api/overview")).json() == []


# ── GET /overview/{job_type} (all runs of one automation) ─────────────────────

async def test_automation_overview_combines_runs_across_jobs(client, db_session):
    p = {"job_type": "tw104_apply", "payload": {"keyword": "x"}}
    j1 = (await client.post("/api/jobs", json={"name": "104 A", **p})).json()
    j2 = (await client.post("/api/jobs", json={"name": "104 B", **p})).json()
    db_session.add(Run(job_id=j1["id"], status="success",
                       started_at=datetime(2026, 1, 1, tzinfo=UTC)))
    db_session.add(Run(job_id=j2["id"], status="failed",
                       started_at=datetime(2026, 1, 2, tzinfo=UTC)))
    db_session.commit()

    data = (await client.get("/api/overview/tw104_apply")).json()
    assert data["job_type"] == "tw104_apply"
    assert data["task_names"][0] == "Start"
    # Both jobs' runs appear together, oldest → newest, each labelled by its job.
    assert [r["status"] for r in data["runs"]] == ["success", "failed"]
    assert {r["job_name"] for r in data["runs"]} == {"104 A", "104 B"}


async def test_automation_overview_unknown_type_empty(client):
    data = (await client.get("/api/overview/web_scraper")).json()
    assert data["runs"] == []
    assert data["task_names"][0] == "Start"  # step scaffold still returned


# ── GET /runs/{id}/steps (per-step log drill-down) ────────────────────────────

async def test_run_steps_returns_per_step_logs(client, db_session):
    job = (await client.post("/api/jobs", json=FORM_PAYLOAD)).json()
    log = json.dumps([
        {"ts": "00:00:01", "msg": "Starting google_form_fill..."},
        {"ts": "00:00:02", "msg": "Payload validated"},
        {"ts": "00:00:03", "msg": "Inspecting Google Form"},
        {"ts": "00:00:04", "msg": "found 5 fields"},
    ])
    run = Run(job_id=job["id"], status="running", log=log,
              started_at=datetime(2026, 1, 1, tzinfo=UTC))
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    data = (await client.get(f"/api/runs/{run.id}/steps")).json()
    assert data["job_type"] == "google_form_fill"
    steps = {s["name"]: s for s in data["steps"]}
    assert [e["msg"] for e in steps["Validate"]["logs"]] == ["Payload validated"]
    inspect_msgs = [e["msg"] for e in steps["Inspect Form"]["logs"]]
    assert "Inspecting Google Form" in inspect_msgs
    assert "found 5 fields" in inspect_msgs        # follow-on line stays in the step
    assert steps["Submit"]["logs"] == []           # not reached


async def test_run_steps_missing_run_404(client):
    resp = await client.get("/api/runs/9999/steps")
    assert resp.status_code == 404


async def test_overview_orders_runs_oldest_to_newest(client, db_session):
    job = (await client.post("/api/jobs", json=FORM_PAYLOAD)).json()
    db_session.add(Run(job_id=job["id"], status="success",
                       started_at=datetime(2026, 1, 1, tzinfo=UTC)))
    db_session.add(Run(job_id=job["id"], status="failed",
                       started_at=datetime(2026, 1, 2, tzinfo=UTC)))
    db_session.commit()

    runs = (await client.get(f"/api/jobs/{job['id']}/overview")).json()["runs"]
    assert [r["status"] for r in runs] == ["success", "failed"]  # oldest → newest
