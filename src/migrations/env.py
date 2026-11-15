"""Alembic environment.

Normally driven by ``src.migrations.run_migrations()``, which hands over an open
connection via ``config.attributes``. When invoked from the ``alembic`` CLI (authoring
a revision) there is none, so it connects to the app's configured ``DATABASE_URL``.

No ``fileConfig()`` call on purpose: it would replace the application's logging setup.
"""

from alembic import context
from sqlmodel import SQLModel

import src.models  # noqa: F401 — registers every table on SQLModel.metadata

target_metadata = SQLModel.metadata


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things in place; batch mode rebuilds the table.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline (--sql) mode is not supported; run against a database.")

_connection = context.config.attributes.get("connection")
if _connection is not None:
    _run(_connection)
else:
    from src.database import engine

    with engine.connect() as _conn:
        _run(_conn)
        _conn.commit()
