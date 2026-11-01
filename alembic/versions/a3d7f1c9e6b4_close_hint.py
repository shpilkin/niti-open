"""action_items.close_hint — почему ассистент предлагает закрыть карточку

Флаг `close_candidate` был, а причины не было: конечный автомат ставил его, когда собеседник
написал после моего обещания, и это отображалось серой точкой без объяснения. Теперь причин
две (собеседник отозвался / месяц без движения), и у секции «Предлагаю закрыть» должно быть
что сказать.

Revision ID: a3d7f1c9e6b4
Revises: f2a6c9d4b8e1
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3d7f1c9e6b4"
down_revision: str | None = "f2a6c9d4b8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("action_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("close_hint", sa.String(), nullable=True))
    # Уже помеченные карточки помечал автомат — единственная причина, которая тогда была.
    op.execute(
        "UPDATE action_items SET close_hint = 'acknowledged' WHERE close_candidate = 1"
    )


def downgrade() -> None:
    with op.batch_alter_table("action_items", schema=None) as batch_op:
        batch_op.drop_column("close_hint")
