"""Browser-login session management endpoints.

Lets an operator refresh the persisted login session for the storage-state
automations (tasker / 104 / Shopee) from the UI instead of SSHing in to run the
one-off ``scripts/*_login.py``. Only useful when the server runs locally — the
headed browser opens on the server's display. See
``src.automation.browser_session``.
"""
from fastapi import APIRouter, HTTPException

from src.automation import browser_session as bs

router = APIRouter()


@router.get("/sessions")
def list_sessions() -> dict:
    """Freshness of every browser-login session + whether login is available."""
    return {"login_enabled": bs.login_enabled(), "sessions": bs.all_status()}


@router.get("/sessions/{name}")
def get_session(name: str) -> dict:
    status = bs.session_status(name)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Unknown automation session '{name}'.")
    return status


@router.post("/sessions/{name}/login", status_code=202)
def refresh_session(name: str) -> dict:
    """Start a headed-browser login for the given automation (background task).
    Poll GET /sessions/{name} for progress and the final result."""
    try:
        return bs.start_login(name)
    except bs.LoginError as exc:
        # Unknown → 404; disabled → 403; already running → 409.
        msg = str(exc)
        if "Unknown" in msg:
            code = 404
        elif "disabled" in msg:
            code = 403
        else:
            code = 409
        raise HTTPException(status_code=code, detail=msg) from exc
