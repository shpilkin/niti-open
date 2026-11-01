"""meetings: soft delete (deleted_at)

A deleted meeting stays as a row: the recorder may deliver the same audio again, and
ext_id dedup must recognise it as this meeting instead of creating a live one. Lists,
counts and routing skip rows with deleted_at set.

Revision ID: c9e5a3b7d2f4
Revises: d4f8b2c6e1a3
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9e5a3b7d2f4"
down_revision: str | None = "d4f8b2c6e1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("meetings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_meetings_deleted_at", ["deleted_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("meetings", schema=None) as batch_op:
        batch_op.drop_index("ix_meetings_deleted_at")
        batch_op.drop_column("deleted_at")
