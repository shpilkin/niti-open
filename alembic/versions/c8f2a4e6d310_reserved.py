"""Ревизия без изменений в этой редакции.
Revision ID: c8f2a4e6d310 · Revises: b6d3f9a1c724
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "c8f2a4e6d310"
down_revision: str | None = "b6d3f9a1c724"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
