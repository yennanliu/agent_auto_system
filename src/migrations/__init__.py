"""Database migrations (Alembic), run programmatically on every startup.

``run_migrations(engine)`` is what ``database.init_db()`` calls. It is idempotent and
handles the three states a database can be in:

* **fresh** (none of our tables): ``upgrade head`` builds the whole schema.
* **legacy** (our tables, but never stamped by Alembic): run the old hand-written
  ``ALTER TABLE`` block one last time, verify the result against the frozen baseline,
  and only then ``stamp`` it as the baseline and ``upgrade head``. A database the
  baseline cannot describe is *not* stamped — startup stops with ``SchemaMismatchError``.
* **versioned** (has an ``alembic_version`` row): ``upgrade head``; a no-op when current.

Alembic is configured in code — there is no dependency on an ``alembic.ini`` or on the
working directory, so this also works inside the Docker image and the frozen desktop
build. The repo-root ``alembic.ini`` exists only for authoring revisions with the CLI.
"""

import logging
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext

from src.migrations.baseline import BASELINE_REVISION, LEGACY_DDL, build_metadata, schema_drift

logger = logging.getLogger(__name__)

SCRIPT_LOCATION = Path(__file__).resolve().parent


class SchemaMismatchError(RuntimeError):
    """A pre-Alembic database does not match the baseline schema, so it was not stamped."""


def alembic_config(connection: sa.Connection | None = None) -> Config:
    cfg = Config()  # no .ini: nothing here depends on the current working directory
    cfg.set_main_option("script_location", SCRIPT_LOCATION.as_posix())
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def _bootstrap_legacy(engine: sa.Engine) -> None:
    """Bring a pre-Alembic database up to the baseline, verify it, and stamp it."""
    with engine.connect() as conn:
        # What init_db() used to do: create any table that does not exist yet …
        build_metadata().create_all(conn)
        conn.commit()
        # … then the additive ALTERs, each allowed to fail ("already exists").
        for ddl in LEGACY_DDL:
            try:
                conn.execute(sa.text(ddl))
                conn.commit()
            except Exception:
                conn.rollback()

        problems = schema_drift(conn)
        conn.rollback()
    if problems:
        raise SchemaMismatchError(
            "This database predates Alembic and does not match the baseline schema "
            f"(revision {BASELINE_REVISION}), so it was NOT stamped and no migration was "
            "run. Fix the differences below (or restore a backup) and restart:\n  - "
            + "\n  - ".join(problems)
        )
    with engine.begin() as conn:
        command.stamp(alembic_config(conn), BASELINE_REVISION)
    logger.info("Existing database verified and stamped as %s", BASELINE_REVISION)


def run_migrations(engine: sa.Engine) -> None:
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
        existing = set(sa.inspect(conn).get_table_names())
        conn.rollback()

    if current is None and existing & set(build_metadata().tables):
        _bootstrap_legacy(engine)

    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
