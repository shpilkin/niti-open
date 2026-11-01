"""action_items: source_kind discriminator, direct account_id, source_ref

Four nullable source FKs (thread / message / event / meeting) meant every consumer
probed them in turn, and a fifth source (Telegram) would have added a fifth column and a
fifth branch everywhere. `source_kind` is the one field to switch on; the FKs stay as the
references they are. `account_id` gives a mailbox to items that never had a thread to
derive it from (Telegram, quick-add) — per-mailbox isolation must hold for them too.
`source_ref` points into a source that has no table («chat_id:message_id»).

Revision ID: 3c7d9e1f5a2b
Revises: f2a8c5d17b93
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3c7d9e1f5a2b"
down_revision: str | None = "f2a8c5d17b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("action_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("source_kind", sa.String(), server_default="mail", nullable=False)
        )
        batch_op.add_column(sa.Column("account_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("source_ref", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_action_items_account", "accounts", ["account_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.create_index("ix_action_items_account_id", ["account_id"], unique=False)
        batch_op.create_index("ix_action_items_source_kind", ["source_kind"], unique=False)
    # Backfill from whichever reference each existing row carries.
    op.execute(
        sa.text(
            "UPDATE action_items SET source_kind = CASE"
            " WHEN source_meeting_id IS NOT NULL THEN 'meeting'"
            " WHEN source_event_id IS NOT NULL THEN 'calendar'"
            " WHEN source_thread_id IS NOT NULL THEN 'mail'"
            " ELSE 'manual' END"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("action_items", schema=None) as batch_op:
        batch_op.drop_index("ix_action_items_source_kind")
        batch_op.drop_index("ix_action_items_account_id")
        batch_op.drop_constraint("fk_action_items_account", type_="foreignkey")
        batch_op.drop_column("source_ref")
        batch_op.drop_column("account_id")
        batch_op.drop_column("source_kind")
