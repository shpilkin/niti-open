"""agent_decisions: индекс по карточке — журнал теперь читают

История карточки (панель обязательства) выбирает записи по action_item_id. Таблица растёт
быстрее всех остальных (больше всего в ней записей разбора по
проектам), а индекса по этой колонке не было: чтение истории сканировало всю таблицу.

Revision ID: f2a6c9d4b8e1
Revises: c5a1e8f37b92
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f2a6c9d4b8e1"
down_revision: str | None = "c5a1e8f37b92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_decisions_action_item_id", "agent_decisions", ["action_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_decisions_action_item_id", table_name="agent_decisions")
