"""widen uq_action_item_meeting to include the title

A mail thread holds ONE evolving obligation per counterparty, so (source, type,
counterparty) is the right key there. A meeting is the opposite: one recording legitimately
produces several distinct tasks for the same person («Иван готовит смету», «Иван бронирует
зал»). With the three-column key the second one could not be stored at all, so the title
joins the key. Idempotency across re-extractions still comes from delete-and-recreate
(§8.5), not from this constraint.

Revision ID: d1f6c8b4e207
Revises: c5a9f2b6d0e4
Create Date: 2026-08-16 16:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'd1f6c8b4e207'
down_revision: str | None = 'c5a9f2b6d0e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('action_items', schema=None) as batch_op:
        batch_op.drop_constraint('uq_action_item_meeting', type_='unique')
        batch_op.create_unique_constraint(
            'uq_action_item_meeting',
            ['source_meeting_id', 'type', 'counterparty', 'title'],
        )


def downgrade() -> None:
    with op.batch_alter_table('action_items', schema=None) as batch_op:
        batch_op.drop_constraint('uq_action_item_meeting', type_='unique')
        batch_op.create_unique_constraint(
            'uq_action_item_meeting', ['source_meeting_id', 'type', 'counterparty']
        )
