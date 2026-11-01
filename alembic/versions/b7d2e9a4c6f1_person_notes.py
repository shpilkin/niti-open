"""people.notes + people.updated_at — user-authored memory about a person

Notes are global across mailboxes (identity does not depend on which mailbox someone
wrote to); they are rendered into prompts as reference data, never as instructions.
No backfill: rows are created lazily when a person is first opened or annotated.

Revision ID: b7d2e9a4c6f1
Revises: b8d4f2e6c1a7
Create Date: 2026-09-04 22:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'b7d2e9a4c6f1'
down_revision: str | None = 'b8d4f2e6c1a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('people', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notes', sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                'updated_at', email_reader.dbtypes.UTCDateTime(timezone=True), nullable=True
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('people', schema=None) as batch_op:
        batch_op.drop_column('updated_at')
        batch_op.drop_column('notes')
