"""Admin-only API: data retention policy, manual purge, per-user export/erase.

See src/retention.py and doc/data-retention.md.
"""
import asyncio
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlmodel import Session

from src import retention
from src.auth import require_admin
from src.database import get_session
from src.models import User

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/admin/retention")
def get_retention():
    """The effective policy (from env) and the last automatic sweep's summary."""
    return {
        "policy": asdict(retention.policy()),
        "last_purge": retention.retention_task.last_result,
    }


@router.post("/admin/retention/purge")
async def purge_now(dry_run: bool = Query(True)):
    """Run a retention sweep now. Defaults to a dry run (counts only); pass
    ``dry_run=false`` to actually delete. Works even when the automatic purge
    is disabled — it uses the configured windows."""
    return await asyncio.to_thread(retention.purge_expired, dry_run=dry_run)


def _get_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/admin/users/{user_id}/export")
def export_user(user_id: int, session: Session = Depends(get_session)):
    """Download a zip of one user's jobs, runs, uploads and report PDFs."""
    user = _get_user(session, user_id)
    data = retention.export_user_data(session, user)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="user-{user.id}-export.zip"'},
    )


@router.delete("/admin/users/{user_id}/data")
def delete_user_data(
    user_id: int,
    confirm: str = Query(..., description="Must equal the user's username"),
    session: Session = Depends(get_session),
):
    """Irreversibly erase one user's jobs, runs and files (the account stays —
    remove it with ``DELETE /admin/users/{id}`` afterwards if wanted)."""
    user = _get_user(session, user_id)
    if confirm != user.username:
        raise HTTPException(status_code=400, detail="confirm must equal the user's username")
    try:
        return retention.delete_user_data(session, user)
    except retention.ActiveRunsError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"User has runs in progress (ids: {exc.run_ids}); cancel them first",
        ) from exc
