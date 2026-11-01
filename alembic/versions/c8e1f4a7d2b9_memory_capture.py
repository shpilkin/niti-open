"""memory_facts: what a captured fact carries — confidence, the quote, who vouched when

Capture (docs/feature-memory-facts.md §3.7) writes facts the owner has not said yes to
yet. Such a row needs: the model's confidence, the words of the source it quoted (the
confirmation screen shows them next to the fact), the space whose sources it came from
(a fact about a person is global once confirmed, but the letter behind the proposal
belongs to one space), the name of a person the address book does not know yet, and two
stamps — when the owner confirmed it and when a source last said it again — that the
«устарело?» mark is computed from.

Revision ID: c8e1f4a7d2b9
Revises: d4b7e2c9f1a6
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = "c8e1f4a7d2b9"
down_revision: str | None = "d4b7e2c9f1a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAMP = email_reader.dbtypes.UTCDateTime


def upgrade() -> None:
    with op.batch_alter_table("memory_facts") as batch:
        batch.add_column(sa.Column("confidence", sa.Float(), nullable=True))
        batch.add_column(sa.Column("quote", sa.Text(), nullable=True))
        batch.add_column(sa.Column("subject_name", sa.String(), nullable=True))
        batch.add_column(sa.Column("captured_in", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("confirmed_at", _STAMP(), nullable=True))
        batch.add_column(sa.Column("last_seen_at", _STAMP(), nullable=True))
        batch.create_foreign_key(
            "fk_memory_facts_captured_in", "spaces", ["captured_in"], ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    # Rows capture wrote stay: the older code reads `active` facts only and never sees a
    # proposal or a rejection, so nothing downstream changes.
    with op.batch_alter_table("memory_facts") as batch:
        batch.drop_constraint("fk_memory_facts_captured_in", type_="foreignkey")
        for name in ("last_seen_at", "confirmed_at", "captured_in", "subject_name",
                     "quote", "confidence"):
            batch.drop_column(name)
