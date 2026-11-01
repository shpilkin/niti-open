"""email read_at (local read state)

Revision ID: d4a1e6b2c7f0
Revises: c9f0a7839ab6
Create Date: 2026-06-29 14:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'd4a1e6b2c7f0'
down_revision: str | None = 'c9f0a7839ab6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('emails', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('read_at', email_reader.dbtypes.UTCDateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('emails', schema=None) as batch_op:
        batch_op.drop_column('read_at')
