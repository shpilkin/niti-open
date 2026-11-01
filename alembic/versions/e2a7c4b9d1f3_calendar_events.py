"""calendar_events + action_items.source_event_id

Revision ID: e2a7c4b9d1f3
Revises: c1e4a7d2f9b0
Create Date: 2026-07-03 13:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'e2a7c4b9d1f3'
down_revision: str | None = 'c1e4a7d2f9b0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'calendar_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('account_id', sa.Uuid(), nullable=True),
        sa.Column('provider_uid', sa.String(), nullable=False),
        sa.Column('start_at', email_reader.dbtypes.UTCDateTime(timezone=True), nullable=False),
        sa.Column('end_at', email_reader.dbtypes.UTCDateTime(timezone=True), nullable=True),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('location', sa.Text(), nullable=True),
        sa.Column('organizer', sa.String(), nullable=True),
        sa.Column('attendees', sa.JSON(), nullable=False),
        sa.Column('cancelled_at', email_reader.dbtypes.UTCDateTime(timezone=True), nullable=True),
        sa.Column('prep_confirmed', sa.Boolean(), nullable=True),
        sa.Column('memo', sa.Text(), nullable=True),
        sa.Column('memo_generated_at', email_reader.dbtypes.UTCDateTime(timezone=True), nullable=True),
        sa.Column('created_at', email_reader.dbtypes.UTCDateTime(timezone=True),
                  server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', email_reader.dbtypes.UTCDateTime(timezone=True),
                  server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'provider_uid', name='uq_calevent_account_uid'),
    )
    with op.batch_alter_table('calendar_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_calendar_events_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_calendar_events_provider_uid'), ['provider_uid'], unique=False)
        batch_op.create_index(batch_op.f('ix_calendar_events_start_at'), ['start_at'], unique=False)

    with op.batch_alter_table('action_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_event_id', sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            'fk_action_items_source_event', 'calendar_events', ['source_event_id'], ['id']
        )
        batch_op.create_unique_constraint('uq_action_item_event', ['source_event_id'])


def downgrade() -> None:
    with op.batch_alter_table('action_items', schema=None) as batch_op:
        batch_op.drop_constraint('uq_action_item_event', type_='unique')
        batch_op.drop_constraint('fk_action_items_source_event', type_='foreignkey')
        batch_op.drop_column('source_event_id')

    with op.batch_alter_table('calendar_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_calendar_events_start_at'))
        batch_op.drop_index(batch_op.f('ix_calendar_events_provider_uid'))
        batch_op.drop_index(batch_op.f('ix_calendar_events_account_id'))
    op.drop_table('calendar_events')
