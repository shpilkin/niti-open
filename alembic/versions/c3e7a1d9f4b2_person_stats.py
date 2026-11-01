"""person_stats — cached memory.stats.person_stats per (space, address)

Boards order rows by the sender history (ongoing conversation, how often and how fast I
answer this person); computing that per row on every render is too slow, so the numbers
are cached here, refreshed at extraction time and by the worker's idle sweep.

Revision ID: c3e7a1d9f4b2
Revises: b7d2e9a4c6f1
Create Date: 2026-09-05 16:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'c3e7a1d9f4b2'
down_revision: str | None = 'b7d2e9a4c6f1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'person_stats',
        sa.Column('space_id', sa.Uuid(), sa.ForeignKey('spaces.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('window_days', sa.Integer(), nullable=False, server_default='90'),
        sa.Column('inbound', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('inbound_to_me', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('outbound', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('replied', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('unanswered', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_inbound', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('median_reply_hours', sa.Float(), nullable=True),
        sa.Column('last_inbound_at', email_reader.dbtypes.UTCDateTime(timezone=True),
                  nullable=True),
        sa.Column('last_outbound_at', email_reader.dbtypes.UTCDateTime(timezone=True),
                  nullable=True),
        sa.Column('first_seen_at', email_reader.dbtypes.UTCDateTime(timezone=True),
                  nullable=True),
        sa.Column('computed_at', email_reader.dbtypes.UTCDateTime(timezone=True),
                  nullable=False),
        sa.PrimaryKeyConstraint('space_id', 'email'),
    )


def downgrade() -> None:
    op.drop_table('person_stats')
