"""Ревизия без изменений в этой редакции.
Revision ID: c5a1e8f37b92 · Revises: b7e4a2c9d581
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "c5a1e8f37b92"
down_revision: str | None = "b7e4a2c9d581"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
