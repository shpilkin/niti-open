"""Подключение модели: флаг «без рассуждений»

Рассуждающая модель тратит на размышления тот же лимит max_tokens, что и на ответ, и при
небольшом лимите ответа не остаётся вовсе. Локальные шлюзы (llama.cpp, vLLM, SGLang)
умеют отключать размышления через шаблон чата; галочка хранится у подключения, потому
что это свойство конкретного шлюза, а не всего приложения.

Revision ID: a1c7f4e9b2d3
Revises: d4f9e2b7a1c6
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a1c7f4e9b2d3"
down_revision = "d4f9e2b7a1c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_connections", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("no_thinking", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("model_connections", schema=None) as batch_op:
        batch_op.drop_column("no_thinking")
