"""Ревизия без изменений в этой редакции.
Revision ID: d41b7c9e25a6 · Revises: c8f2a4e6d310
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "d41b7c9e25a6"
down_revision: str | None = "c8f2a4e6d310"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
