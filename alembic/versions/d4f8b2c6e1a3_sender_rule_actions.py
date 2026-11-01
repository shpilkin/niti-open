"""sender_rules.action / value — a sender rule can do more than attach a label

action: 'label' (the old behaviour, label_id set), 'ignore' (the sender's mail is low
value: no extraction), 'delegate' (cards from the sender become TO_DELEGATE to `value`).
label_id becomes nullable for the non-label kinds. Learned rules are only ever created
after the user confirms a proposal (docs/feature-memory.md §5).

Revision ID: d4f8b2c6e1a3
Revises: c3e7a1d9f4b2
Create Date: 2026-09-05 17:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'd4f8b2c6e1a3'
down_revision: str | None = 'c3e7a1d9f4b2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('sender_rules', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('action', sa.String(), nullable=False, server_default='label')
        )
        batch_op.add_column(sa.Column('value', sa.String(), nullable=True))
        batch_op.alter_column('label_id', existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM sender_rules WHERE action != 'label'")
    with op.batch_alter_table('sender_rules', schema=None) as batch_op:
        batch_op.alter_column('label_id', existing_type=sa.Uuid(), nullable=False)
        batch_op.drop_column('value')
        batch_op.drop_column('action')
