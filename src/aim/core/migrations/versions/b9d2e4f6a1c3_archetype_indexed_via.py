"""archetype indexed_via

Revision ID: b9d2e4f6a1c3
Revises: a7c1e9b3d5f2
Create Date: 2026-09-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "b9d2e4f6a1c3"
down_revision: str | None = "a7c1e9b3d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("archetypeindex", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("indexed_via", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
    # Backfill from the path, mirroring origins.derive_origin's rationale for
    # legacy NULL rows: pre-narrowing discovery indexed instruction files from
    # ANY directory, so a non-canonical row is exactly what an explicit link now
    # represents — without this, the first reindex would silently drop every
    # legacy non-canonical archetype a project may have selected.
    op.execute(
        sa.text(
            "UPDATE archetypeindex SET indexed_via = CASE "
            "WHEN (source_path LIKE 'instructions/%' "
            "      AND source_path NOT LIKE 'instructions/%/%') "
            "  OR (source_path LIKE '.aim/instructions/%' "
            "      AND source_path NOT LIKE '.aim/instructions/%/%') "
            "THEN 'discovered' ELSE 'link' END"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("archetypeindex", schema=None) as batch_op:
        batch_op.drop_column("indexed_via")
