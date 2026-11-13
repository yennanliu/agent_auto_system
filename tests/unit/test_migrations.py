"""Alembic migrations run by init_db(): fresh, legacy (pre-Alembic), versioned, mismatch."""

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import src.models  # noqa: F401 — registers the tables on SQLModel.metadata
from src.migrations import SchemaMismatchError, alembic_config, run_migrations
from src.migrations.baseline import BASELINE_REVISION, build_metadata, schema_drift

HEAD = ScriptDirectory.from_config(alembic_config()).get_current_head()

# The schema of the very first release: no users, no harness/eval columns, no indexes.
_ORIGINAL_SCHEMA = (
    "CREATE TABLE job (id INTEGER NOT NULL PRIMARY KEY, name VARCHAR NOT NULL, "
    "job_type VARCHAR NOT NULL, payload VARCHAR NOT NULL, created_at DATETIME NOT NULL)",
    "CREATE TABLE run (id INTEGER NOT NULL PRIMARY KEY, job_id INTEGER NOT NULL, "
    "status VARCHAR NOT NULL, result VARCHAR, started_at DATETIME NOT NULL, "
    "finished_at DATETIME, FOREIGN KEY(job_id) REFERENCES job (id))",
    "INSERT INTO job VALUES (1, 'old job', 'google_form_fill', '{}', '2025-01-01 00:00:00')",
    "INSERT INTO run VALUES (1, 1, 'success', '{\"ok\": true}', "
    "'2025-01-01 00:00:00', '2025-01-01 00:00:30')",
)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # A different CWD than the repo root: locating the scripts must not depend on it.
    monkeypatch.chdir(tmp_path)
    eng = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    yield eng
    eng.dispose()


def _revision(engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _tables(engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _exec(engine, *statements: str) -> None:
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(sa.text(stmt))


def _drift(engine) -> list[str]:
    with engine.connect() as conn:
        return schema_drift(conn)


def test_fresh_database_is_built_by_upgrade_head(engine):
    run_migrations(engine)

    assert set(build_metadata().tables) <= _tables(engine)
    assert _revision(engine) == HEAD
    if HEAD == BASELINE_REVISION:  # later revisions legitimately move away from it
        assert _drift(engine) == []


def test_fresh_in_memory_database(tmp_path):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    run_migrations(eng)
    assert _revision(eng) == HEAD


def test_legacy_database_is_upgraded_verified_and_stamped(engine):
    _exec(engine, *_ORIGINAL_SCHEMA)

    run_migrations(engine)

    assert _revision(engine) == HEAD
    assert set(build_metadata().tables) <= _tables(engine)
    with engine.connect() as conn:
        # existing rows survive, legacy defaults apply, and the duration backfill ran
        row = conn.execute(
            sa.text("SELECT status, tokens_in, models_attempted, duration_secs FROM run")
        ).one()
        assert row.status == "success"
        assert row.tokens_in == 0
        assert row.models_attempted == 1
        assert row.duration_secs == pytest.approx(30.0, abs=0.01)
        assert conn.execute(sa.text("SELECT name FROM job")).scalar_one() == "old job"
    indexes = {ix["name"] for ix in sa.inspect(engine).get_indexes("run")}
    assert {"ix_run_job_id", "ix_run_status", "ix_run_started_at", "ix_run_user_id"} <= indexes


def test_database_created_by_previous_release_is_stamped(engine):
    """What main produced right before Alembic: create_all() from the models."""
    build_metadata().create_all(engine)
    assert _revision(engine) is None

    run_migrations(engine)

    assert _revision(engine) == HEAD


def test_second_start_is_a_noop(engine):
    _exec(engine, *_ORIGINAL_SCHEMA)
    run_migrations(engine)
    with engine.connect() as conn:
        before = conn.execute(sa.text("SELECT sql FROM sqlite_master ORDER BY name")).all()

    run_migrations(engine)
    run_migrations(engine)

    with engine.connect() as conn:
        after = conn.execute(sa.text("SELECT sql FROM sqlite_master ORDER BY name")).all()
        assert conn.execute(sa.text("SELECT count(*) FROM run")).scalar_one() == 1
    assert after == before
    assert _revision(engine) == HEAD


def test_stamped_database_skips_the_legacy_path(engine, mocker):
    run_migrations(engine)
    legacy = mocker.patch("src.migrations._bootstrap_legacy")

    run_migrations(engine)

    legacy.assert_not_called()


@pytest.mark.parametrize(
    ("ddl", "expected"),
    [
        ("ALTER TABLE run ADD COLUMN mystery VARCHAR", "run.mystery"),
        ("ALTER TABLE job ADD COLUMN schedule INTEGER", "job.schedule"),
    ],
    ids=["unknown-column", "wrong-type"],
)
def test_mismatching_legacy_database_is_not_stamped(engine, ddl, expected):
    _exec(engine, *_ORIGINAL_SCHEMA, ddl)

    with pytest.raises(SchemaMismatchError) as exc:
        run_migrations(engine)

    assert expected in str(exc.value)
    assert "NOT stamped" in str(exc.value)
    assert _revision(engine) is None
    assert "alembic_version" not in _tables(engine)
    # and it keeps refusing on the next start rather than stamping the second time
    with pytest.raises(SchemaMismatchError):
        run_migrations(engine)


def test_schema_drift_reports_missing_pieces(engine):
    build_metadata().create_all(engine)
    _exec(engine, "DROP INDEX ix_run_status", "DROP TABLE setting")

    assert _drift(engine) == [
        "table 'setting' is missing",
        "index 'ix_run_status' on 'run' is missing",
    ]


def test_models_match_migration_head(engine):
    """Drift guard: a model change without an Alembic revision fails here."""
    run_migrations(engine)

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, SQLModel.metadata)

    assert diff == [], (
        "src/models.py and the migrations disagree — add a revision under "
        f"src/migrations/versions/ (see doc/dev-notes.md): {diff}"
    )


def test_init_db_runs_migrations_on_the_app_engine(engine, mocker):
    import src.database as db

    mocker.patch.object(db, "engine", engine)
    db.init_db()
    db.init_db()

    assert _revision(engine) == HEAD
    assert db.reconcile_stale_runs() == 0
