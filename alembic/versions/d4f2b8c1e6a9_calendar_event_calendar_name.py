"""Календарь события: имя календаря внутри ящика — по нему цвет в сетке

У ящика бывает несколько календарей («Мои события», «резерв», «Не забыть»); раньше все
они красились цветом ящика (docs/feature-calendar.md §5). Старые строки остаются NULL,
пока синхронизация не пришлёт событие снова.

Revision ID: d4f2b8c1e6a9
Revises: c3e8a1f6d2b7
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d4f2b8c1e6a9"
down_revision = "c3e8a1f6d2b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("calendar_events") as batch:
        batch.add_column(sa.Column("calendar_name", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("calendar_events") as batch:
        batch.drop_column("calendar_name")
