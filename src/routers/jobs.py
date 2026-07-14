import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from src.auth import assert_can_run, require_user
from src.automation.cron_utils import is_valid_cron, next_fire, normalize_cron
from src.automation.flow_steps import (
    infer_step_states,
    pipeline_step_states,
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
    # Most recent run per job (single pass, newest first).
    runs = session.exec(
        select(Run).where(Run.job_id.in_(job_ids)).order_by(Run.started_at.desc())
    ).all()
    last_run: dict[int, Run] = {}
    run_counts: dict[int, int] = {}
    for r in runs:
        run_counts[r.job_id] = run_counts.get(r.job_id, 0) + 1
        last_run.setdefault(r.job_id, r)

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


@router.get("/jobs/{job_id}/overview")
def job_overview(
    job_id: int,
    limit: int = 30,
    session: Session = Depends(get_session),
):
    """Airflow-style grid data for a job: recent runs × per-step status.

    Returns the ordered ``task_names`` (grid rows) and, per run (grid columns),
    the status of each step derived from the run's progress log.
    """
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    limit = max(1, min(limit, 100))

    runs = session.exec(
        select(Run).where(Run.job_id == job_id).order_by(Run.started_at.desc()).limit(limit)
    ).all()

    # Fixed rows come from the flow definition; pipeline rows are per-run dynamic,
    # so we take the union (longest wins) across the returned runs.
    task_names = step_labels(job.job_type)
    run_rows = []
    for r in reversed(runs):  # oldest → newest so the grid reads left-to-right by time
        steps = _run_step_states(job.job_type, r)
        if job.job_type == "pipeline" and len(steps) > len(task_names):
            task_names = [s["name"] for s in steps]
        run_rows.append({
            "run_id": r.id,
            "status": r.status,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "duration_secs": r.duration_secs,
            "steps": steps,
        })

    return {
        "job": {"id": job.id, "name": job.name, "job_type": job.job_type, "schedule": job.schedule},
        "task_names": task_names,
        "runs": run_rows,
    }
