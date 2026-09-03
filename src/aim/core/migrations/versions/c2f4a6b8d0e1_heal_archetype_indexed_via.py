"""heal archetype indexed_via

Revision ID: c2f4a6b8d0e1
Revises: b9d2e4f6a1c3
Create Date: 2026-09-03 00:00:00.000000

Revision b9d2e4f6a1c3 shipped in TWO shapes during development: an early
version added an `origin` column to archetypeindex; it was later rewritten in
place to add `indexed_via` (+ backfill) instead. A database that migrated
under the early shape is stamped b9d2e4f6a1c3 but lacks the `indexed_via`
column, so every archetype query crashes with "no such column". This revision
reconciles by inspecting the live schema:

- `indexed_via` present  -> nothing to heal (rewritten shape ran).
- `origin` present       -> rename it to `indexed_via` (same value vocabulary)
                            and run the backfill the early shape lacked.
- neither                -> add the column, then backfill.

Lesson enforced going forward: a migration file is immutable the moment it may
have been applied anywhere — dev databases included. Fix mistakes with a new
revision, never by editing an applied one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "c2f4a6b8d0e1"
down_revision: str | None = "b9d2e4f6a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL = (
    "UPDATE archetypeindex SET indexed_via = CASE "
    "WHEN (source_path LIKE 'instructions/%' "
    "      AND source_path NOT LIKE 'instructions/%/%') "
    "  OR (source_path LIKE '.aim/instructions/%' "
    "      AND source_path NOT LIKE '.aim/instructions/%/%') "
    "THEN 'discovered' ELSE 'link' END "
    "WHERE indexed_via IS NULL"
)


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("archetypeindex")}
    if "indexed_via" in columns:
        return  # the rewritten b9d2e4f6a1c3 ran; schema is already correct
    with op.batch_alter_table("archetypeindex", schema=None) as batch_op:
        if "origin" in columns:
            batch_op.alter_column("origin", new_column_name="indexed_via")
        else:  # pragma: no cover — no known path stamps b9 without either column
            batch_op.add_column(
                sa.Column("indexed_via", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
            )
    op.execute(sa.text(_BACKFILL))


def downgrade() -> None:
    # Healing is one-way by design: the schema this converges on is exactly
    # what the (final) b9d2e4f6a1c3 produces, and ITS downgrade drops the
    # column. There is no meaningful earlier state to return to from here.
    pass
