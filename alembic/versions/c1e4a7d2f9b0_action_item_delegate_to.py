"""action_items.delegate_to (suggested delegation target)

Revision ID: c1e4a7d2f9b0
Revises: b3f1c0d2e4a6
Create Date: 2026-07-03 09:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'c1e4a7d2f9b0'
down_revision: str | None = 'b3f1c0d2e4a6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('action_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('delegate_to', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('action_items', schema=None) as batch_op:
        batch_op.drop_column('delegate_to')
