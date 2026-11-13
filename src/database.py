import os
from pathlib import Path

from dotenv import load_dotenv
from sqlmodel import Session, create_engine

load_dotenv()

_db_url = os.getenv("DATABASE_URL", "sqlite:///./data/auto.db")
# check_same_thread is a SQLite-only driver option; other drivers reject it.
_connect_args = {"check_same_thread": False} if _db_url.startswith("sqlite") else {}
engine = create_engine(_db_url, connect_args=_connect_args)


def get_engine():
    return engine


def init_db():
    """Bring the database to the latest schema. Runs on every startup; idempotent.

    Schema changes live in Alembic revisions under ``src/migrations/versions/`` —
    see ``src/migrations/__init__.py`` for the fresh / legacy / versioned handling.
    """
    if _db_url.startswith("sqlite:///./"):
        Path("data").mkdir(exist_ok=True)
    from src.migrations import run_migrations
    run_migrations(engine)


def reconcile_stale_runs() -> int:
    """Mark any runs stuck in pending/running as failed (e.g. after server restart)."""
    from sqlalchemy import text
    error = '{"error": "Server restarted while run was in progress"}'
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "UPDATE run SET status='failed', result=:result "
                "WHERE status IN ('running', 'pending')"
            ),
            {"result": error},
        )
        conn.commit()
        return result.rowcount


def get_session():
    with Session(engine) as session:
        yield session
