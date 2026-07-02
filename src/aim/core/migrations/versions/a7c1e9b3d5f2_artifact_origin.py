"""artifact origin

Revision ID: a7c1e9b3d5f2
Revises: f6b8d0c2a4e1
Create Date: 2026-07-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "a7c1e9b3d5f2"
down_revision: str | None = "f6b8d0c2a4e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Index tables that gain provenance columns. No backfill: NULL origin marks a
# legacy row written by pre-origin code (re-derived or rebuilt on next index).
_ORIGIN_TABLES = ("skillindex", "agentindex", "ruleindex")


def upgrade() -> None:
    for table in _ORIGIN_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("origin", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
            )
            batch_op.add_column(
                sa.Column("owning_plugin", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
            )


def downgrade() -> None:
    for table in _ORIGIN_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("owning_plugin")
            batch_op.drop_column("origin")
