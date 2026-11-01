"""action_items: counterparty and delegate_to are one line of at most 120 characters

The model had written its whole reasoning into the counterparty field of a few cards
(up to 4 500 characters). New values are capped in the extraction schema; this trims
what is already stored, so the pane stops showing an essay where a name belongs. The
trimmed text is not kept — downgrade is a no-op.

Revision ID: b8d4f2e6c1a7
Revises: a7c3e9d1f5b2
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d4f2e6c1a7"
down_revision: str | None = "a7c3e9d1f5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIMIT = 120


def _one_line(v: str | None) -> str | None:
    lines = [ln.strip() for ln in (v or "").splitlines()]
    lines = [ln for ln in lines if ln]
    return (lines[0][:LIMIT].rstrip() or None) if lines else None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, counterparty, delegate_to FROM action_items "
            "WHERE length(counterparty) > :n OR instr(counterparty, char(10)) > 0 "
            "OR length(delegate_to) > :n OR instr(delegate_to, char(10)) > 0"
        ),
        {"n": LIMIT},
    ).fetchall()
    for id_, cp, dl in rows:
        conn.execute(
            sa.text(
                "UPDATE action_items SET counterparty = :cp, delegate_to = :dl WHERE id = :id"
            ),
            {"cp": _one_line(cp), "dl": _one_line(dl), "id": id_},
        )


def downgrade() -> None:
    pass
