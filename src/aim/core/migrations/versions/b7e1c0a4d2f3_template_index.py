"""template index

Revision ID: b7e1c0a4d2f3
Revises: d7424c089c0e
Create Date: 2026-06-22 09:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "b7e1c0a4d2f3"
down_revision: str | None = "d7424c089c0e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "templateindex",
        sa.Column("qualified_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("repo_alias", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("template_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("template_toml_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("indexed_at_sha", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("qualified_name"),
    )
    with op.batch_alter_table("templateindex", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_templateindex_template_name"), ["template_name"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_templateindex_repo_alias"), ["repo_alias"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("templateindex", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_templateindex_repo_alias"))
        batch_op.drop_index(batch_op.f("ix_templateindex_template_name"))

    op.drop_table("templateindex")
