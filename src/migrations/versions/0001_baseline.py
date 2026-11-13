"""Baseline: the schema as it stood when Alembic was adopted.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-13

Fresh databases are built by this revision. Databases that predate Alembic are
verified against the same frozen definition and then stamped with it — see
``src/migrations/__init__.py``.
"""

from alembic import op

from src.migrations.baseline import BASELINE_REVISION, build_metadata

revision = BASELINE_REVISION
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    build_metadata().create_all(op.get_bind())


def downgrade() -> None:
    build_metadata().drop_all(op.get_bind())
