"""drafts — недописанные письма

Revision ID: a3d7f1c95b28
Revises: d41b7c9e25a6
Create Date: 2026-09-09 12:00:00.000000

Брошенный композер терял текст. Черновик живёт в своей таблице.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes
from alembic import op

revision: str = "a3d7f1c95b28"
down_revision: str | None = "d41b7c9e25a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("to_line", sa.String(), nullable=False, server_default=""),
        sa.Column("cc_line", sa.String(), nullable=False, server_default=""),
        sa.Column("subject", sa.String(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("in_reply_to", sa.String(), nullable=False, server_default=""),
        sa.Column("references", sa.Text(), nullable=False, server_default=""),
        sa.Column("forward_email_id", sa.Uuid(), nullable=True),
        sa.Column("back_url", sa.String(), nullable=False, server_default=""),
        sa.Column(
            "created_at", email_reader.dbtypes.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True,
        ),
        sa.Column(
            "updated_at", email_reader.dbtypes.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["forward_email_id"], ["emails.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_drafts_account_id", "drafts", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_drafts_account_id", table_name="drafts")
    op.drop_table("drafts")
