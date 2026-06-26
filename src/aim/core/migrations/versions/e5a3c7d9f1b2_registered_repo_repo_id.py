"""registeredrepo source-agnostic repo_id

Adds the stable ``repo_id`` identity column (``sha256(normalize_repo_url(url))``
truncated to 16 hex chars) to ``registeredrepo`` and a unique index over it. The
value is backfilled from each row's existing ``url`` so the column is non-null and
the dedup invariant (one row per upstream repo) holds for already-registered repos.

Revision ID: e5a3c7d9f1b2
Revises: d4f2a1b6c8e0
Create Date: 2026-06-26 09:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "e5a3c7d9f1b2"
down_revision: str | None = "d4f2a1b6c8e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add nullable first, backfill, then enforce non-null + the unique index — SQLite
    # cannot add a NOT NULL column to a non-empty table without a default.
    with op.batch_alter_table("registeredrepo", schema=None) as batch_op:
        batch_op.add_column(sa.Column("repo_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    from aim.core.policy import repo_id_for_url

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT alias, url FROM registeredrepo")).fetchall()
    for alias, url in rows:
        bind.execute(
            sa.text("UPDATE registeredrepo SET repo_id = :rid WHERE alias = :alias"),
            {"rid": repo_id_for_url(url), "alias": alias},
        )

    # A DB created before identity-dedup (the old alias-keyed `add`) can hold two rows
    # for the same upstream repo under different aliases — they backfill to the same
    # repo_id. A UNIQUE index would abort this upgrade, and since every command runs
    # migrations on first DB touch, the user could not even run `aim repo remove` to
    # recover (deadlock). So make the index unique only when the backfill is already
    # collision-free — the universal case for DBs created after the dedup fix. When a
    # legacy duplicate exists, fall back to a plain index; `repos.add`/`get_by_id`
    # keep enforcing one-row-per-identity at the application layer.
    backfilled = bind.execute(sa.text("SELECT repo_id FROM registeredrepo")).fetchall()
    ids = [row[0] for row in backfilled]
    unique = len(set(ids)) == len(ids)

    with op.batch_alter_table("registeredrepo", schema=None) as batch_op:
        batch_op.alter_column(
            "repo_id", existing_type=sqlmodel.sql.sqltypes.AutoString(), nullable=False
        )
        batch_op.create_index(batch_op.f("ix_registeredrepo_repo_id"), ["repo_id"], unique=unique)


def downgrade() -> None:
    with op.batch_alter_table("registeredrepo", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_registeredrepo_repo_id"))
        batch_op.drop_column("repo_id")
