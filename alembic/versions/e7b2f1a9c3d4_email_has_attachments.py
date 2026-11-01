"""email has_attachments flag

Revision ID: e7b2f1a9c3d4
Revises: d4a1e6b2c7f0
Create Date: 2026-06-30 10:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'e7b2f1a9c3d4'
down_revision: str | None = 'd4a1e6b2c7f0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('emails', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('has_attachments', sa.Boolean(), server_default=sa.text('0'), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table('emails', schema=None) as batch_op:
        batch_op.drop_column('has_attachments')
