"""Событие календаря: UID из iCalendar — по нему одна встреча узнаётся под новым ключом

Revision ID: f3a1d7c5b9e8
Revises: e7b3c9d1a4f2
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f3a1d7c5b9e8"
down_revision = "e7b3c9d1a4f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("calendar_events") as batch:
        batch.add_column(sa.Column("external_uid", sa.String(), nullable=True))
        batch.create_index("ix_calendar_events_external_uid", ["external_uid"])


def downgrade() -> None:
    with op.batch_alter_table("calendar_events") as batch:
        batch.drop_index("ix_calendar_events_external_uid")
        batch.drop_column("external_uid")
