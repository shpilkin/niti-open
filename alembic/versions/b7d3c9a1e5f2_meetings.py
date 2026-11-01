"""meetings (transcripts handed over by the recorder app)

Revision ID: b7d3c9a1e5f2
Revises: a4c7e1f9d2b3
Create Date: 2026-08-16 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'b7d3c9a1e5f2'
down_revision: str | None = 'a4c7e1f9d2b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'meetings',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('ext_id', sa.String(), nullable=False),
        sa.Column('started_at', email_reader.dbtypes.UTCDateTime(timezone=True), nullable=False),
        sa.Column('duration_sec', sa.Integer(), nullable=True),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('participants', sa.JSON(), nullable=False),
        sa.Column('transcript_path', sa.Text(), nullable=True),
        sa.Column('srt_path', sa.Text(), nullable=True),
        sa.Column('audio_path', sa.Text(), nullable=True),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('routed_at', email_reader.dbtypes.UTCDateTime(timezone=True), nullable=True),
        sa.Column('created_at', email_reader.dbtypes.UTCDateTime(timezone=True),
                  server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', email_reader.dbtypes.UTCDateTime(timezone=True),
                  server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_meetings_ext_id'), ['ext_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_meetings_started_at'), ['started_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_meetings_started_at'))
        batch_op.drop_index(batch_op.f('ix_meetings_ext_id'))
    op.drop_table('meetings')
