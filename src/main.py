import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from src import telemetry as _tel
from src.auth import require_user
from src.database import get_engine, init_db, reconcile_stale_runs
from src.routers import (
    admin,
    auth,
    cover_letters,
    jobs,
    oauth,
    runs,
    sessions,
    system,
    uploads,
)
from src.security import DEV_SECRET as _DEV_SECRET
from src.security import SessionCookieMiddleware, enforce_secure_defaults

logger = logging.getLogger(__name__)

# Cookie signing + key-derivation secret (settings_store derives the Fernet key
# from it, so rotating it logs everyone out and makes stored API keys
# undecryptable). The dev fallback only survives startup in desktop mode or with
# ALLOW_INSECURE_DEFAULTS=1 — see security.enforce_secure_defaults.
APP_SECRET = os.getenv("APP_SECRET") or _DEV_SECRET


def _seed_admin() -> None:
    """Create the first admin from env if no users exist yet."""
    from sqlmodel import Session, select

    from src.auth import hash_password
    from src.models import User

    with Session(get_engine()) as session:
        if session.exec(select(User)).first():
            return
        # `or`: a blank value (e.g. `ADMIN_PASSWORD=` in .env) means unset, not "".
        username = os.getenv("ADMIN_USERNAME") or "admin"
        password = os.getenv("ADMIN_PASSWORD") or "admin"
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
    _tel.setup(app)
    # Off the event loop — these do network I/O (client auth + score-config
    # registration) and must not block startup if Langfuse is slow/unreachable.
    import asyncio

    from src.automation.harness import langfuse_tracer
    await asyncio.to_thread(langfuse_tracer.get_client)  # initialise + log once if configured
    await asyncio.to_thread(langfuse_tracer.ensure_score_configs)  # typed score schemas (idempotent)
    init_db()
    enforce_secure_defaults(get_engine())  # raises → uvicorn exits (server mode only)
    _seed_admin()
    stale = reconcile_stale_runs()
    if stale:
        logger.warning("Marked %d stale run(s) as failed on startup", stale)

    # Cron scheduler: fires runs for jobs that carry a schedule. Opt-out via
    # SCHEDULER_ENABLED=0 (e.g. for tests or single-shot deployments).
    from src.automation.scheduler import scheduler
    if os.getenv("SCHEDULER_ENABLED", "1") != "0":
        scheduler.start()

    yield

    await scheduler.stop()
    langfuse_tracer.flush()  # drain buffered traces on shutdown


app = FastAPI(title="Agent Auto System", lifespan=lifespan)

if APP_SECRET == _DEV_SECRET:
    logger.warning(
        "APP_SECRET is unset; using an insecure dev default. Set APP_SECRET in "
        "production — sessions are signed with it."
    )
# `Secure` cookie flag: SESSION_COOKIE_SECURE=1 behind TLS; default follows the request scheme.
app.add_middleware(SessionCookieMiddleware, secret_key=APP_SECRET)

# Auth endpoints stay open (login must be reachable without a session).
app.include_router(auth.router, prefix="/api")
# SSO/OAuth login + provider discovery — also open (pre-login redirect flow).
app.include_router(oauth.router, prefix="/api")

# Admin router enforces require_admin internally (every route).
app.include_router(admin.router, prefix="/api")

# Everything else requires a logged-in user.
_gated = [Depends(require_user)]
app.include_router(jobs.router, prefix="/api", dependencies=_gated)
app.include_router(runs.router, prefix="/api", dependencies=_gated)
app.include_router(system.router, prefix="/api", dependencies=_gated)
app.include_router(sessions.router, prefix="/api", dependencies=_gated)
app.include_router(uploads.router, prefix="/api", dependencies=_gated)
app.include_router(cover_letters.router, prefix="/api", dependencies=_gated)

if Path("ui").exists():
    app.mount("/ui", StaticFiles(directory="ui"), name="ui")


@app.get("/")
def root():
    return FileResponse("ui/index.html")


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
