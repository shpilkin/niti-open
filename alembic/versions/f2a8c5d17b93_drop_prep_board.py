"""drop the calendar prep board: delete its live items

The «К подготовке» board and the gate that filled it are gone. Nothing creates
MEETING_PREP any more, and nothing resolves the live ones either — the auto-resolve of
past meetings lived in the same gate. Left alone they would drift into «Сегодня» by
due_date with no board to close them from, so they are deleted here by explicit choice.

Only LIVE rows. The closed ones (done/dismissed) are history and stay: the type is still
in ActionType and still renders in the archive.

Revision ID: f2a8c5d17b93
Revises: e9b4a7c3f158
Create Date: 2026-08-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f2a8c5d17b93"
down_revision = "e9b4a7c3f158"
branch_labels = None
depends_on = None

_LIVE = ("open", "in_progress", "overdue", "snoozed")


def upgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM action_items WHERE type = 'MEETING_PREP'"
            f" AND status IN ({','.join(repr(s) for s in _LIVE)})"
        )
    )


def downgrade() -> None:
    # The rows are gone; the gate that produced them is gone too. Nothing to restore.
    pass
