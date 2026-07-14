import asyncio
import csv
import io
import json
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import func, text
from sqlmodel import Session, select

from src.auth import assert_can_run, require_user
from src.automation.launcher import launch_run
from src.automation.registry import cancel as cancel_task
from src.automation.report_render import REPORTS_ROOT
from src.database import get_engine, get_session
from src.models import Job, Run, User

logger = logging.getLogger(__name__)
router = APIRouter()


def _assert_run_visible(run: Run, user: User) -> None:
    """Non-admins may only touch their own runs; hide others as 404."""
    if not user.is_admin and run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Run not found")


@router.post("/jobs/{job_id}/run", status_code=202)
async def trigger_run(
    job_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    assert_can_run(user, job.job_type)

    payload = json.loads(job.payload)
    run_id = launch_run(job_id, job.job_type, payload, user_id=user.id, trigger="manual")
    logger.info("Triggered run_id=%d for job_id=%d (%s)", run_id, job_id, job.job_type)

    return {"run_id": run_id, "status": "pending"}


@router.post("/runs/{run_id}/cancel", status_code=200)
async def cancel_run(
    run_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    _assert_run_visible(run, user)
    if run.status not in ("pending", "running"):
        raise HTTPException(status_code=409, detail="Run is not active")

    was_cancelled = cancel_task(run_id)
    if was_cancelled:
        run.status = "failed"
        run.result = json.dumps({"error": "Cancelled by user"})
        run.finished_at = datetime.now(UTC)
        session.add(run)
        session.commit()

    return {"cancelled": was_cancelled, "run_id": run_id}


def _parse_day(value: str | None) -> datetime | None:
    """Parse an ISO date/datetime filter value; return None if empty/unparseable.

    A bare date ("2026-07-05") parses timezone-naive; we pin it to UTC so it
    compares cleanly against the tz-aware Run.started_at column (Postgres rejects
    naive-vs-aware comparisons)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@router.get("/runs")
def list_runs(
    response: Response,
    offset: int = 0,
    limit: int = 50,
    job_type: str | None = None,
    job_id: int | None = None,
    status: str | None = None,
    started_after: str | None = None,
    started_before: str | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    stmt = select(Run).order_by(Run.started_at.desc())
    if not user.is_admin:
        stmt = stmt.where(Run.user_id == user.id)  # users see only their own runs
    if status:
        stmt = stmt.where(Run.status == status)
    if job_id is not None:
        stmt = stmt.where(Run.job_id == job_id)
    if job_type:
        # Run has no job_type column; filter via the jobs of that type.
        stmt = stmt.where(Run.job_id.in_(select(Job.id).where(Job.job_type == job_type)))
    if (after := _parse_day(started_after)) is not None:
        stmt = stmt.where(Run.started_at >= after)
    if (before := _parse_day(started_before)) is not None:
        # A bare "YYYY-MM-DD" means "up to and including that day".
        if started_before and len(started_before) <= 10:
            before += timedelta(days=1)
        stmt = stmt.where(Run.started_at < before)
    # Total matching rows (before offset/limit) drives client-side page numbering.
    total = session.scalar(stmt.order_by(None).with_only_columns(func.count(Run.id))) or 0
    response.headers["X-Total-Count"] = str(total)
    runs = session.exec(stmt.offset(offset).limit(limit)).all()
    if not runs:
        return []
    job_ids = list({r.job_id for r in runs})
    jobs = {j.id: j for j in session.exec(select(Job).where(Job.id.in_(job_ids))).all()}
    # Owner usernames (only useful to admins, who see everyone's runs).
    owner_ids = list({r.user_id for r in runs if r.user_id is not None})
    owners = (
        {u.id: u.username for u in session.exec(select(User).where(User.id.in_(owner_ids))).all()}
        if owner_ids else {}
    )
    result = []
    for run in runs:
        job = jobs.get(run.job_id)
        result.append({
            "id": run.id,
            "job_id": run.job_id,
            "job_name": job.name if job else f"job {run.job_id}",
            "job_type": job.job_type if job else "unknown",
            "owner": owners.get(run.user_id),
            "status": run.status,
            "result": run.result,
            "log": run.log,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "llm_provider": run.llm_provider,
            "llm_model": run.llm_model,
            "served_model": run.served_model,
            "fallback_used": bool(run.fallback_used),
            "models_attempted": run.models_attempted or 1,
            "tokens_in": run.tokens_in or 0,
            "tokens_out": run.tokens_out or 0,
            "cost_usd": run.cost_usd or 0.0,
            "retry_count": run.retry_count or 0,
            "duration_secs": run.duration_secs,
            "validation_passed": run.validation_passed,
            "validation_reason": run.validation_reason,
            "eval_score": run.eval_score,
            "eval_confidence": run.eval_confidence,
            "eval_notes": run.eval_notes,
            "eval_method": run.eval_method,
        })
    return result


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(
    run_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    _assert_run_visible(run, user)
    if run.status in ("pending", "running"):
        raise HTTPException(status_code=409, detail="Cannot delete a run that is in progress")
    session.delete(run)
    session.commit()


@router.get("/runs/{run_id}")
def get_run(
    run_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    _assert_run_visible(run, user)
    return run


@router.get("/runs/{run_id}/report.pdf")
def get_run_report(run_id: int):
    """Serve the generated PDF report for a run (profit_health_check)."""
    path = REPORTS_ROOT / f"{run_id}.pdf"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No PDF report for this run")
    return FileResponse(path, media_type="application/pdf", filename=f"profit-health-{run_id}.pdf")


# Column order for the email_collect CSV export — most useful for outreach first.
_LEAD_CSV_FIELDS = [
    "company", "email", "confidence", "icp_fit", "hook", "category", "phone",
    "website", "address", "region", "source", "mx_found", "smtp_status",
    "reason", "maps_url",
]


@router.get("/runs/{run_id}/leads.csv")
def get_run_leads_csv(
    run_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    """Export a email_collect run's leads as CSV (UTF-8 BOM for Excel/中文)."""
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    _assert_run_visible(run, user)
    job = session.get(Job, run.job_id)
    if not job or job.job_type != "email_collect":
        raise HTTPException(status_code=400, detail="Run is not a email_collect job")

    try:
        result = json.loads(run.result) if run.result else {}
    except (json.JSONDecodeError, TypeError):
        result = {}
    leads = result.get("leads") or []

    buf = io.StringIO()
    buf.write("\ufeff")  # BOM so Excel reads UTF-8 (Chinese) correctly
    writer = csv.DictWriter(buf, fieldnames=_LEAD_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        writer.writerow({k: lead.get(k, "") for k in _LEAD_CSV_FIELDS})

    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="leads-{run_id}.csv"'},
    )


@router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    _assert_run_visible(run, user)
    _engine = get_engine()

    async def event_generator():
        last_status = None
        last_log_count = 0
        try:
            while True:
                with Session(_engine) as s:
                    run = s.get(Run, run_id)

                if run is None:
                    yield f"data: {json.dumps({'error': 'run not found'})}\n\n"
                    break

                current_log: list = []
                if run.log:
                    try:
                        current_log = json.loads(run.log)
                    except json.JSONDecodeError:
                        pass

                status_changed = run.status != last_status
                new_entries = current_log[last_log_count:]

                if status_changed or new_entries:
                    last_status = run.status
                    last_log_count = len(current_log)

                    event: dict = {"status": run.status}
                    if new_entries:
                        event["new_logs"] = new_entries
                    if run.result:
                        try:
                            event["result"] = json.loads(run.result)
                        except json.JSONDecodeError:
                            event["result"] = run.result
                    if run.eval_score is not None:
                        event["eval_score"] = run.eval_score
                        event["eval_confidence"] = run.eval_confidence
                        event["eval_notes"] = run.eval_notes
                        event["eval_method"] = run.eval_method
                    yield f"data: {json.dumps(event)}\n\n"

                if run.status in ("success", "failed"):
                    break

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/runs", status_code=200)
def bulk_delete_runs(
    ids: str | None = Query(None, description="Comma-separated run IDs"),
    delete_all: bool = Query(False),
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    if delete_all:
        stmt = select(Run).where(Run.status.not_in(["pending", "running"]))
    elif ids:
        id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
        if not id_list:
            return {"deleted": 0}
        stmt = select(Run).where(Run.id.in_(id_list)).where(
            Run.status.not_in(["pending", "running"])
        )
    else:
        return {"deleted": 0}
    if not user.is_admin:
        stmt = stmt.where(Run.user_id == user.id)  # users only delete their own
    runs = session.exec(stmt).all()

    for run in runs:
        session.delete(run)
    session.commit()
    return {"deleted": len(runs)}


@router.get("/stats")
def get_stats(days: int = 7):
    engine = get_engine()
    today = datetime.now(UTC).date()
    # Selectable trend window; clamp to a small allowlist to keep the query cheap.
    if days not in (7, 14, 30):
        days = 7

    with engine.connect() as conn:
        total_runs = conn.execute(text("SELECT COUNT(*) FROM run")).scalar() or 0
        if not total_runs:
            return _empty_stats(days)

        # Overall counts, token totals, and average duration
        row = conn.execute(text("""
            SELECT
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END),
                SUM(CASE WHEN status IN ('pending','running') THEN 1 ELSE 0 END),
                SUM(COALESCE(tokens_in,  0)),
                SUM(COALESCE(tokens_out, 0)),
                SUM(COALESCE(cost_usd,   0.0)),
                AVG(CASE WHEN finished_at IS NOT NULL AND status IN ('success','failed')
                    THEN (julianday(finished_at) - julianday(started_at)) * 86400 END),
                AVG(eval_score),
                AVG(eval_confidence)
            FROM run
        """)).fetchone()

        n_success, n_failed, n_active = row[0] or 0, row[1] or 0, row[2] or 0
        total_tokens_in  = row[3] or 0
        total_tokens_out = row[4] or 0
        total_cost       = row[5] or 0.0
        avg_dur          = round(row[6], 1) if row[6] is not None else 0
        avg_eval_score   = round(row[7], 1) if row[7] is not None else None
        avg_eval_conf    = round(row[8], 2) if row[8] is not None else None

        # Eval trust (how the scores were produced) + retry reliability.
        # A trustworthy quality number is one graded by an independent LLM judge;
        # heuristic fallbacks and self-graded runs (flagged in eval_notes) do not count.
        trust = conn.execute(text("""
            SELECT
                SUM(CASE WHEN eval_score IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN eval_score IS NOT NULL AND eval_method='llm' THEN 1 ELSE 0 END),
                SUM(CASE WHEN eval_score IS NOT NULL AND COALESCE(eval_method,'heuristic')!='llm' THEN 1 ELSE 0 END),
                SUM(CASE WHEN eval_score IS NOT NULL AND eval_method='llm'
                    AND (eval_notes IS NULL OR eval_notes NOT LIKE '%self-graded%') THEN 1 ELSE 0 END),
                SUM(CASE WHEN COALESCE(retry_count, 0) > 0 THEN 1 ELSE 0 END),
                AVG(COALESCE(retry_count, 0))
            FROM run
        """)).fetchone()

        eval_scored      = trust[0] or 0
        eval_llm         = trust[1] or 0
        eval_heuristic   = trust[2] or 0
        eval_independent = trust[3] or 0
        retried_runs     = trust[4] or 0
        avg_retries      = round(trust[5], 2) if trust[5] is not None else 0

        # Cross-model fallback + validation gate reliability (Tier 2 columns).
        rel = conn.execute(text("""
            SELECT
                SUM(CASE WHEN fallback_used=1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN validation_passed IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN validation_passed=0 THEN 1 ELSE 0 END)
            FROM run
        """)).fetchone()
        fallback_runs    = rel[0] or 0
        validated_runs   = rel[1] or 0
        validation_fails = rel[2] or 0

        # Duration percentiles from the stored/backfilled column. SQLite has no
        # PERCENTILE(); fetch the durations once and compute nth-of-count in
        # Python — one roundtrip and one sort instead of a count + two sorts.
        durations = sorted(
            row[0] for row in conn.execute(text(
                "SELECT duration_secs FROM run WHERE duration_secs IS NOT NULL"
            ))
        )
        n_dur = len(durations)

        def _percentile(p: float):
            if not n_dur:
                return None
            v = durations[min(int(p * n_dur), n_dur - 1)]
            return round(v, 1) if v is not None else None

        p50_dur = _percentile(0.50)
        p95_dur = _percentile(0.95)

        # By type: counts + weighted average duration per job_type
        type_rows = conn.execute(text("""
            SELECT
                COALESCE(j.job_type, 'unknown') AS jtype,
                r.status,
                COUNT(*) AS cnt,
                SUM(CASE WHEN r.finished_at IS NOT NULL AND r.status IN ('success','failed')
                    THEN (julianday(r.finished_at) - julianday(r.started_at)) * 86400 ELSE 0 END) AS sum_dur,
                SUM(CASE WHEN r.finished_at IS NOT NULL AND r.status IN ('success','failed')
                    THEN 1 ELSE 0 END) AS cnt_dur,
                SUM(CASE WHEN COALESCE(r.retry_count, 0) > 0 THEN 1 ELSE 0 END) AS n_retried
            FROM run r
            LEFT JOIN job j ON r.job_id = j.id
            GROUP BY jtype, r.status
        """)).fetchall()

        by_type: dict = {}
        for jtype, status, cnt, sum_dur, cnt_dur, n_retried in type_rows:
            bt = by_type.setdefault(jtype, {
                "total": 0, "success": 0, "failed": 0, "pending": 0, "running": 0,
                "retried": 0, "_sum_dur": 0.0, "_cnt_dur": 0,
            })
            bt["total"] += cnt
            bt[status] = bt.get(status, 0) + cnt
            bt["retried"] += n_retried or 0
            bt["_sum_dur"] += sum_dur or 0.0
            bt["_cnt_dur"] += cnt_dur or 0

        for bt in by_type.values():
            sd, cd = bt.pop("_sum_dur"), bt.pop("_cnt_dur")
            bt["avg_duration"] = round(sd / cd, 1) if cd else 0

        # By provider
        prov_rows = conn.execute(text("""
            SELECT
                COALESCE(llm_provider, 'unknown') AS provider,
                COUNT(*) AS runs,
                SUM(COALESCE(tokens_in,  0)),
                SUM(COALESCE(tokens_out, 0)),
                SUM(COALESCE(cost_usd,   0.0))
            FROM run
            GROUP BY COALESCE(llm_provider, 'unknown')
        """)).fetchall()

        by_provider: dict = {}
        for provider, runs, ti, to_, cost in prov_rows:
            by_provider[provider] = {
                "runs": runs, "tokens_in": ti or 0, "tokens_out": to_ or 0,
                "cost_usd": round(cost or 0.0, 6), "models": [],
            }

        model_rows = conn.execute(text("""
            SELECT DISTINCT COALESCE(llm_provider, 'unknown'), llm_model
            FROM run WHERE llm_model IS NOT NULL
        """)).fetchall()
        for provider, model in model_rows:
            if provider in by_provider:
                by_provider[provider]["models"].append(model)
        for bp in by_provider.values():
            bp["models"].sort()

        # Per-model detailed breakdown
        model_detail_rows = conn.execute(text("""
            SELECT
                COALESCE(llm_provider, 'unknown') AS provider,
                COALESCE(llm_model, 'unknown') AS model,
                COUNT(*) AS runs,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS n_success,
                SUM(COALESCE(tokens_in, 0)) AS ti,
                SUM(COALESCE(tokens_out, 0)) AS tot,
                SUM(COALESCE(cost_usd, 0.0)) AS cost,
                AVG(CASE WHEN finished_at IS NOT NULL AND status IN ('success','failed')
                    THEN (julianday(finished_at) - julianday(started_at)) * 86400 END) AS avg_dur,
                AVG(eval_score) AS avg_score,
                AVG(eval_confidence) AS avg_conf,
                SUM(CASE WHEN eval_score IS NOT NULL THEN 1 ELSE 0 END) AS n_scored,
                SUM(CASE WHEN eval_score IS NOT NULL AND eval_method='llm' THEN 1 ELSE 0 END) AS n_llm,
                SUM(CASE WHEN COALESCE(retry_count, 0) > 0 THEN 1 ELSE 0 END) AS n_retried,
                SUM(CASE WHEN fallback_used=1 THEN 1 ELSE 0 END) AS n_fallback
            FROM run
            WHERE llm_model IS NOT NULL
            GROUP BY provider, model
            ORDER BY cost DESC
        """)).fetchall()

        by_model: dict = {}
        for (prov, model, runs, n_success, ti, tot, cost, avg_dur, avg_score,
             avg_conf, n_scored, n_llm, n_retried, n_fallback) in model_detail_rows:
            n_success = n_success or 0
            cost = round(cost or 0.0, 6)
            avg_score_r = round(avg_score, 1) if avg_score is not None else None
            by_model.setdefault(prov, []).append({
                "model": model,
                "runs": runs,
                "success": n_success,
                "tokens_in": ti or 0,
                "tokens_out": tot or 0,
                "cost_usd": cost,
                "avg_duration": round(avg_dur, 1) if avg_dur else 0,
                "avg_eval_score": avg_score_r,
                "avg_eval_confidence": round(avg_conf, 2) if avg_conf is not None else None,
                "scored": n_scored or 0,
                "llm_judged": n_llm or 0,
                "retried": n_retried or 0,
                "fallback": n_fallback or 0,
                # Cost efficiency: $/successful run and $ per quality point (Tier 3)
                "cost_per_success": round(cost / n_success, 6) if n_success else None,
                "cost_per_quality": round(cost / (n_success * avg_score_r), 8)
                    if n_success and avg_score_r else None,
            })

        # N-day trend with cost / tokens / quality per day (only days with runs;
        # gaps filled below). Window is the selectable `days` param. Compare the
        # raw (indexed) started_at against a Python-computed boundary so the
        # ix_run_started_at index is usable — DATE(started_at) would not be.
        # started_at is stored as an ISO string, so a date-string bound compares
        # correctly (and dodges the Python 3.12 sqlite date-adapter deprecation).
        since_date = (today - timedelta(days=days - 1)).isoformat()
        trend_rows = conn.execute(text("""
            SELECT
                DATE(started_at) AS day,
                COUNT(*) AS total,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END),
                SUM(COALESCE(cost_usd, 0.0)),
                SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)),
                AVG(eval_score)
            FROM run
            WHERE started_at >= :since
            GROUP BY DATE(started_at)
        """), {"since": since_date}).fetchall()

        trend_map = {
            row[0]: {
                "total": row[1], "success": row[2] or 0, "failed": row[3] or 0,
                "cost": round(row[4] or 0.0, 6),
                "tokens": row[5] or 0,
                "avg_score": round(row[6], 1) if row[6] is not None else None,
            }
            for row in trend_rows
        }

    n_completed = n_success + n_failed
    # Weekday label alone collides across a 14/30-day window; include the date.
    label_fmt = "%a" if days <= 7 else "%m/%d"
    trend = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        ds = d.isoformat()
        bucket = trend_map.get(ds, {"total": 0, "success": 0, "failed": 0,
                                    "cost": 0.0, "tokens": 0, "avg_score": None})
        trend.append({"date": ds, "label": d.strftime(label_fmt), **bucket})

    return {
        "total_runs": total_runs,
        "success": n_success,
        "failed": n_failed,
        "active": n_active,
        "success_rate": round(n_success / n_completed * 100, 1) if n_completed else 0,
        "avg_duration_secs": avg_dur,
        "avg_eval_score": avg_eval_score,
        "avg_eval_confidence": avg_eval_conf,
        "eval_scored": eval_scored,
        "eval_llm": eval_llm,
        "eval_heuristic": eval_heuristic,
        "eval_independent": eval_independent,
        "eval_independent_rate": round(eval_independent / eval_scored * 100, 1) if eval_scored else None,
        "eval_llm_rate": round(eval_llm / eval_scored * 100, 1) if eval_scored else None,
        "retried_runs": retried_runs,
        "retry_rate": round(retried_runs / total_runs * 100, 1) if total_runs else 0,
        "avg_retries": avg_retries,
        "fallback_runs": fallback_runs,
        "fallback_rate": round(fallback_runs / total_runs * 100, 1) if total_runs else 0,
        "validated_runs": validated_runs,
        "validation_fails": validation_fails,
        "validation_pass_rate": round((validated_runs - validation_fails) / validated_runs * 100, 1)
            if validated_runs else None,
        "p50_duration_secs": p50_dur,
        "p95_duration_secs": p95_dur,
        "trend_days": days,
        "by_type": by_type,
        "trend": trend,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "total_tokens": total_tokens_in + total_tokens_out,
        "total_cost_usd": round(total_cost, 6),
        "by_provider": by_provider,
        "by_model": by_model,
    }


def _empty_stats(days: int = 7):
    today = datetime.now(UTC).date()
    label_fmt = "%a" if days <= 7 else "%m/%d"
    trend = [
        {"date": d.isoformat(), "label": d.strftime(label_fmt), "total": 0, "success": 0,
         "failed": 0, "cost": 0.0, "tokens": 0, "avg_score": None}
        for i in range(days - 1, -1, -1)
        for d in (today - timedelta(days=i),)
    ]
    return {
        "total_runs": 0, "success": 0, "failed": 0, "active": 0,
        "success_rate": 0, "avg_duration_secs": 0,
        "avg_eval_score": None, "avg_eval_confidence": None,
        "eval_scored": 0, "eval_llm": 0, "eval_heuristic": 0, "eval_independent": 0,
        "eval_independent_rate": None, "eval_llm_rate": None,
        "retried_runs": 0, "retry_rate": 0, "avg_retries": 0,
        "fallback_runs": 0, "fallback_rate": 0, "validated_runs": 0,
        "validation_fails": 0, "validation_pass_rate": None,
        "p50_duration_secs": None, "p95_duration_secs": None, "trend_days": days,
        "by_type": {}, "trend": trend,
        "total_tokens_in": 0, "total_tokens_out": 0, "total_tokens": 0,
        "total_cost_usd": 0.0, "by_provider": {}, "by_model": {},
    }
