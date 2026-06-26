"""plugin index composite PK (qualified_name, flavor)

Lets the same plugin name coexist under different kinds in one repo. pluginindex
is a rebuilt-on-index cache, so the table is dropped and recreated rather than
migrated in place.

Revision ID: d4f2a1b6c8e0
Revises: c3a1b2d4e5f6
Create Date: 2026-06-26 01:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "d4f2a1b6c8e0"
down_revision: str | None = "c3a1b2d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create(pk: tuple[str, ...]) -> None:
    op.create_table(
        "pluginindex",
        sa.Column("qualified_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("flavor", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("repo_alias", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("plugin_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("marketplace_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("version", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("category", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("keywords", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("indexed_at_sha", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint(*pk),
    )
    with op.batch_alter_table("pluginindex", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_pluginindex_plugin_name"), ["plugin_name"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_pluginindex_repo_alias"), ["repo_alias"], unique=False)


def upgrade() -> None:
    op.drop_table("pluginindex")  # cache; rows are rebuilt on next `repo refresh`/`sync`
    _create(("qualified_name", "flavor"))


def downgrade() -> None:
    op.drop_table("pluginindex")
    _create(("qualified_name",))
