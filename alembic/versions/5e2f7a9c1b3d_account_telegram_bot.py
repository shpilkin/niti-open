"""accounts: a Telegram bot per mailbox (token + allowed chat id)

Decision §7.3 of docs/ux-tasks.md: a separate bot for each mailbox, so a task sent to a
bot has an owner mailbox by construction and per-mailbox isolation holds without a
«personal» pseudo-mailbox. The token is stored encrypted like the mailbox password.

Revision ID: 5e2f7a9c1b3d
Revises: 3c7d9e1f5a2b
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5e2f7a9c1b3d"
down_revision: str | None = "3c7d9e1f5a2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("telegram_token_enc", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_column("telegram_chat_id")
        batch_op.drop_column("telegram_token_enc")
