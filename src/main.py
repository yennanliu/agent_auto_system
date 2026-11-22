import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from src import telemetry as _tel
from src.auth import require_user
from src.database import get_engine, init_db
from src.logging_setup import RequestContextMiddleware, setup_logging
from src.routers import (
    admin,
    auth,
    cover_letters,
    jobs,
    oauth,
    retention,
    runs,
    sessions,
    system,
    uploads,
)

setup_logging()  # LOG_FORMAT=json|text (default text = no-op), LOG_LEVEL
logger = logging.getLogger(__name__)

# Cookie signing + (later) key-derivation secret. A fixed dev default is used when
# unset so local runs work out of the box; warn loudly because rotating it logs
# everyone out and (in phase 3) makes stored API keys undecryptable.
_DEV_SECRET = "dev-insecure-change-me"
APP_SECRET = os.getenv("APP_SECRET") or _DEV_SECRET


def _seed_admin() -> None:
    """Create the first admin from env if no users exist yet."""
    from sqlmodel import Session, select

    from src.auth import hash_password
    from src.models import User

    with Session(get_engine()) as session:
        if session.exec(select(User)).first():
            return
        username = os.getenv("ADMIN_USERNAME", "admin")
        password = os.getenv("ADMIN_PASSWORD", "admin")
        session.add(
            User(
                username=username,
                password_hash=hash_password(password),
                is_admin=True,
                is_active=True,
                allowed_automations="*",
            )
        )
        session.commit()
        if password == "admin":
            logger.warning(
                "Seeded admin user '%s' with the DEFAULT password 'admin'. "
                "Set ADMIN_PASSWORD (or change it in the admin page) immediately.",
                username,
            )
        else:
            logger.info("Seeded admin user '%s' from ADMIN_USERNAME/ADMIN_PASSWORD.", username)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()  # again: uvicorn.run(app) configures its loggers after import
    _tel.setup(app)
    # Off the event loop — these do network I/O (client auth + score-config
    # registration) and must not block startup if Langfuse is slow/unreachable.
    import asyncio

    from src.automation.harness import langfuse_tracer
    await asyncio.to_thread(langfuse_tracer.get_client)  # initialise + log once if configured
    await asyncio.to_thread(langfuse_tracer.ensure_score_configs)  # typed score schemas (idempotent)
    init_db()
    _seed_admin()
    # In-flight runs from before the restart: resume the checkpointed, resumable
    # ones; mark the rest failed (see src/automation/resume.py).
    from src.automation.resume import recover_stale_runs
    resumed, stale = recover_stale_runs()
    if resumed:
        logger.warning("Resumed %d stale run(s) from checkpoint on startup: %s", len(resumed), resumed)
    if stale:
        logger.warning("Marked %d stale run(s) as failed on startup", stale)

    # Cron scheduler: fires runs for jobs that carry a schedule. Opt-out via
    # SCHEDULER_ENABLED=0 (e.g. for tests or single-shot deployments).
    from src.automation.scheduler import scheduler
    if os.getenv("SCHEDULER_ENABLED", "1") != "0":
        scheduler.start()

    # Data-retention purge (daily). Opt-in: a no-op unless RETENTION_ENABLED=1.
    from src.retention import retention_task
    retention_task.start()

    yield

    await retention_task.stop()
    await scheduler.stop()
    langfuse_tracer.flush()  # drain buffered traces on shutdown


app = FastAPI(title="Agent Auto System", lifespan=lifespan)

if APP_SECRET == _DEV_SECRET:
    logger.warning(
        "APP_SECRET is unset; using an insecure dev default. Set APP_SECRET in "
        "production — sessions are signed with it."
    )
# Added before SessionMiddleware so it runs *inside* it and can read the session.
app.add_middleware(RequestContextMiddleware)
app.add_middleware(SessionMiddleware, secret_key=APP_SECRET, https_only=False)

# Auth endpoints stay open (login must be reachable without a session).
app.include_router(auth.router, prefix="/api")
# SSO/OAuth login + provider discovery — also open (pre-login redirect flow).
app.include_router(oauth.router, prefix="/api")

# Admin router enforces require_admin internally (every route).
app.include_router(admin.router, prefix="/api")
app.include_router(retention.router, prefix="/api")  # admin-only (require_admin inside)

# Everything else requires a logged-in user.
_gated = [Depends(require_user)]
app.include_router(jobs.router, prefix="/api", dependencies=_gated)
app.include_router(runs.router, prefix="/api", dependencies=_gated)
app.include_router(system.router, prefix="/api", dependencies=_gated)
app.include_router(sessions.router, prefix="/api", dependencies=_gated)
app.include_router(uploads.router, prefix="/api", dependencies=_gated)
app.include_router(cover_letters.router, prefix="/api", dependencies=_gated)

# The UI is a set of native ES modules (ui/js/*.js, no build step). Browsers refuse a
# module script that isn't served with a JavaScript MIME type, and some hosts (notably
# Windows, via the registry) map .js to text/plain — so pin it rather than trust the OS.
mimetypes.add_type("text/javascript", ".js")


class _RevalidatedStatic(StaticFiles):
    """Static UI files that the browser must revalidate on every load.

    Without a Cache-Control header browsers cache heuristically, so after an upgrade a
    client could mix a fresh module with a stale one whose exports no longer match —
    which fails the whole module graph. ``no-cache`` still allows caching, but forces an
    ETag/Last-Modified check (a cheap 304) so the module set is always consistent.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


if Path("ui").exists():
    app.mount("/ui", _RevalidatedStatic(directory="ui"), name="ui")


@app.get("/")
def root():
    # Same revalidation as /ui: a stale cached index.html would still reference the
    # pre-split /ui/app.js (now gone) or an outdated module entry point.
    return FileResponse("ui/index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    db_ok = False
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        logger.exception("Health check: DB connectivity failed")

    from src import settings_store
    from src.automation.harness.provider import _CATALOG
    providers = {
        name: settings_store.llm_key_status(name, cfg["env"])["configured"]
        for name, cfg in _CATALOG.items()
    }

    from src.automation.harness import langfuse_tracer
    return {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "providers": providers,
        "langfuse": langfuse_tracer.is_configured(),
    }
