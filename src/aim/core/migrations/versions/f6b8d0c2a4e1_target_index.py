"""target index

Revision ID: f6b8d0c2a4e1
Revises: e5a3c7d9f1b2
Create Date: 2026-06-28 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "f6b8d0c2a4e1"
down_revision: str | None = "e5a3c7d9f1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "targetindex",
        sa.Column("qualified_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("repo_alias", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("target_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("target_toml_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("indexed_at_sha", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("qualified_name"),
    )
    with op.batch_alter_table("targetindex", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_targetindex_target_name"), ["target_name"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_targetindex_repo_alias"), ["repo_alias"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("targetindex", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_targetindex_repo_alias"))
        batch_op.drop_index(batch_op.f("ix_targetindex_target_name"))

    op.drop_table("targetindex")
