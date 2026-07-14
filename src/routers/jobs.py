import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from src.auth import assert_can_run, require_user
from src.automation.cron_utils import is_valid_cron, next_fire, normalize_cron
from src.automation.flow_steps import (
    infer_step_states,
    pipeline_step_states,
    run_step_logs,
    step_labels,
)
from src.database import get_session
from src.models import Job, Run, User

router = APIRouter()


class JobCreate(BaseModel):
    name: str
    job_type: str = "google_form_fill"
    payload: dict
    schedule: str | None = None  # cron expression, e.g. "0 8 * * *"


class JobUpdate(BaseModel):
    name: str | None = None
    # Sentinel-free: `schedule=None` clears the schedule; omit the field to leave
    # it unchanged. Pydantic's model_fields_set distinguishes the two.
    schedule: str | None = None


def _validated_schedule(schedule: str | None) -> str | None:
    """Normalize + validate a cron schedule. Empty → None (manual only)."""
    if schedule is None or not str(schedule).strip():
        return None
    if not is_valid_cron(schedule):
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {schedule!r}")
    return normalize_cron(schedule)


@router.get("/jobs")
def list_jobs(session: Session = Depends(get_session)):
    return session.exec(select(Job).order_by(Job.created_at.desc())).all()


@router.post("/jobs", status_code=201)
def create_job(
    data: JobCreate,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    assert_can_run(user, data.job_type)
    schedule = _validated_schedule(data.schedule)
    job = Job(
        name=data.name,
        job_type=data.job_type,
        payload=json.dumps(data.payload),
        schedule=schedule,
        created_by=user.id,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.get("/jobs/{job_id}")
def get_job(job_id: int, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.patch("/jobs/{job_id}")
def update_job(
    job_id: int,
    data: JobUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    assert_can_run(user, job.job_type)
    fields = data.model_fields_set
    if "name" in fields and data.name is not None:
        job.name = data.name
    if "schedule" in fields:
        job.schedule = _validated_schedule(data.schedule)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: int, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    session.delete(job)
    session.commit()


@router.get("/schedules")
def list_schedules(session: Session = Depends(get_session)):
    """All jobs with a cron schedule, plus next fire time and last-run summary."""
    jobs = session.exec(
        select(Job).where(Job.schedule.is_not(None)).order_by(Job.created_at.desc())
    ).all()
    if not jobs:
        return []

    job_ids = [j.id for j in jobs]
    # Run counts per job — DB-side aggregation (no full-table load into memory).
    run_counts = {
        job_id: cnt
        for job_id, cnt in session.exec(
            select(Run.job_id, func.count(Run.id))
            .where(Run.job_id.in_(job_ids))
            .group_by(Run.job_id)
        ).all()
    }
    # Latest run per job: fetch only the max-id row per job (id is monotonic, so
    # max id == most recent), then hydrate just those rows.
    subq = (
        select(func.max(Run.id))
        .where(Run.job_id.in_(job_ids))
        .group_by(Run.job_id)
        .subquery()
    )
    last_run = {
        r.job_id: r
        for r in session.exec(select(Run).where(Run.id.in_(select(subq)))).all()
    }

    now = datetime.now(UTC)
    out = []
    for j in jobs:
        try:
            nxt = next_fire(j.schedule, now).isoformat() if is_valid_cron(j.schedule) else None
        except ValueError:
            nxt = None
        lr = last_run.get(j.id)
        out.append({
            "job_id": j.id,
            "name": j.name,
            "job_type": j.job_type,
            "schedule": j.schedule,
            "valid": is_valid_cron(j.schedule),
            "next_run_at": nxt,
            "run_count": run_counts.get(j.id, 0),
            "last_run": {
                "id": lr.id, "status": lr.status,
                "started_at": lr.started_at, "finished_at": lr.finished_at,
            } if lr else None,
        })
    return out


def _run_step_states(job_type: str, run: Run) -> list[dict]:
    try:
        logs = json.loads(run.log) if run.log else []
    except (json.JSONDecodeError, TypeError):
        logs = []
    if job_type == "pipeline":
        return pipeline_step_states(logs, run.status)
    return infer_step_states(job_type, logs, run.status)


def _build_grid(job_type: str, runs_desc: list[Run], job_names: dict[int, str]) -> dict:
    """Shared grid builder: ordered ``task_names`` (rows) + per-run step states
    (columns). ``runs_desc`` is newest-first; the grid is emitted oldest→newest so
    it reads left-to-right by time."""
    # Fixed rows come from the flow definition; pipeline rows are per-run dynamic,
    # so we take the union (longest wins) across the returned runs.
    task_names = step_labels(job_type)
    run_rows = []
    for r in reversed(runs_desc):
        steps = _run_step_states(job_type, r)
        if job_type == "pipeline" and len(steps) > len(task_names):
            task_names = [s["name"] for s in steps]
        run_rows.append({
            "run_id": r.id,
            "job_id": r.job_id,
            "job_name": job_names.get(r.job_id, f"job {r.job_id}"),
            "status": r.status,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "duration_secs": r.duration_secs,
            "steps": steps,
        })
    return {"task_names": task_names, "runs": run_rows}


@router.get("/overview")
def overview_index(session: Session = Depends(get_session)):
    """Automation types that have at least one run, with run counts + last status.

    Drives the Task Overview left-hand list: one entry per automation (job_type),
    so all runs of the same automation can be compared together on one grid.
    """
    rows = session.exec(
        select(
            Job.job_type,
            func.count(Run.id),
            func.max(Run.started_at),
        )
        .join(Run, Run.job_id == Job.id)
        .group_by(Job.job_type)
    ).all()
    out = [
        {"job_type": jt, "run_count": n, "last_run_at": last}
        for jt, n, last in rows
    ]
    out.sort(key=lambda r: r["run_count"], reverse=True)
    return out


@router.get("/overview/{job_type}")
def automation_overview(
    job_type: str,
    limit: int = 40,
    session: Session = Depends(get_session),
):
    """Airflow-style grid for ALL runs of one automation type (across every job of
    that type), so runs can be compared side by side. Columns = runs, rows = steps.
    """
    limit = max(1, min(limit, 200))
    jobs = session.exec(select(Job).where(Job.job_type == job_type)).all()
    job_names = {j.id: j.name for j in jobs}

    runs: list[Run] = []
    if job_names:
        runs = list(session.exec(
            select(Run)
            .where(Run.job_id.in_(list(job_names)))
            .order_by(Run.id.desc())  # monotonic recency; NULL-safe across SQLite/Postgres
            .limit(limit)
        ).all())

    grid = _build_grid(job_type, runs, job_names)
    return {"job_type": job_type, **grid}


@router.get("/runs/{run_id}/steps")
def run_steps(
    run_id: int,
    session: Session = Depends(get_session),
):
    """Per-step breakdown of a single run: each step's name, status, and the log
    entries emitted during it. Powers the Task Overview drill-down (click a cell)."""
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    job = session.get(Job, run.job_id)
    job_type = job.job_type if job else "unknown"
    try:
        logs = json.loads(run.log) if run.log else []
    except (json.JSONDecodeError, TypeError):
        logs = []
    return {
        "run_id": run_id,
        "job_type": job_type,
        "status": run.status,
        "steps": run_step_logs(job_type, logs, run.status),
    }


@router.get("/jobs/{job_id}/overview")
def job_overview(
    job_id: int,
    limit: int = 30,
    session: Session = Depends(get_session),
):
    """Airflow-style grid data for a single job: recent runs × per-step status."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    limit = max(1, min(limit, 100))

    runs = list(session.exec(
        select(Run).where(Run.job_id == job_id).order_by(Run.id.desc()).limit(limit)
    ).all())

    grid = _build_grid(job.job_type, runs, {job.id: job.name})
    return {
        "job": {"id": job.id, "name": job.name, "job_type": job.job_type, "schedule": job.schedule},
        **grid,
    }
