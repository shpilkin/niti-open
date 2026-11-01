"""meeting routing: project_meetings, project opt-in/aliases, action_items.source_meeting_id

Revision ID: c5a9f2b6d0e4
Revises: b7d3c9a1e5f2
Create Date: 2026-08-16 15:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'c5a9f2b6d0e4'
down_revision: str | None = 'b7d3c9a1e5f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'project_meetings',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('meeting_id', sa.Uuid(), nullable=False),
        sa.Column('source', sa.String(), server_default='auto', nullable=False),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', email_reader.dbtypes.UTCDateTime(timezone=True),
                  server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'meeting_id', name='uq_project_meeting'),
    )
    with op.batch_alter_table('project_meetings', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_project_meetings_project_id'), ['project_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_project_meetings_meeting_id'), ['meeting_id'], unique=False
        )

    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'include_meetings', sa.Boolean(), server_default=sa.false(), nullable=False
        ))
        batch_op.add_column(sa.Column('aliases', sa.JSON(), nullable=True))
    # Existing rows get an empty alias list (the column is NOT NULL in the model; JSON
    # server defaults aren't portable, so backfill and then tighten).
    op.execute("UPDATE projects SET aliases = '[]' WHERE aliases IS NULL")
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.alter_column('aliases', existing_type=sa.JSON(), nullable=False)

    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('suggestions', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('route_note', sa.Text(), nullable=True))
    op.execute("UPDATE meetings SET suggestions = '[]' WHERE suggestions IS NULL")
    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.alter_column('suggestions', existing_type=sa.JSON(), nullable=False)

    with op.batch_alter_table('action_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_meeting_id', sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            'fk_action_items_source_meeting', 'meetings', ['source_meeting_id'], ['id']
        )
        batch_op.create_unique_constraint(
            'uq_action_item_meeting', ['source_meeting_id', 'type', 'counterparty']
        )


def downgrade() -> None:
    with op.batch_alter_table('action_items', schema=None) as batch_op:
        batch_op.drop_constraint('uq_action_item_meeting', type_='unique')
        batch_op.drop_constraint('fk_action_items_source_meeting', type_='foreignkey')
        batch_op.drop_column('source_meeting_id')

    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.drop_column('route_note')
        batch_op.drop_column('suggestions')

    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('aliases')
        batch_op.drop_column('include_meetings')

    with op.batch_alter_table('project_meetings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_project_meetings_meeting_id'))
        batch_op.drop_index(batch_op.f('ix_project_meetings_project_id'))
    op.drop_table('project_meetings')
