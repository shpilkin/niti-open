"""action_items.resolved_by (provenance) + composite event uniqueness

resolved_by ('user' | 'auto') records who resolved an item — user resolutions are
final, auto ones may be revived. uq_action_item_event widens to (source_event_id,
type, counterparty) so meeting-debrief commitments can share the event link with
the MEETING_PREP card.

Revision ID: f3b8d5c2a9e1
Revises: e2a7c4b9d1f3
Create Date: 2026-07-04 10:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'f3b8d5c2a9e1'
down_revision: str | None = 'e2a7c4b9d1f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('action_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('resolved_by', sa.String(), nullable=True))
        batch_op.drop_constraint('uq_action_item_event', type_='unique')
        batch_op.create_unique_constraint(
            'uq_action_item_event', ['source_event_id', 'type', 'counterparty']
        )


def downgrade() -> None:
    with op.batch_alter_table('action_items', schema=None) as batch_op:
        batch_op.drop_constraint('uq_action_item_event', type_='unique')
        batch_op.create_unique_constraint('uq_action_item_event', ['source_event_id'])
        batch_op.drop_column('resolved_by')
