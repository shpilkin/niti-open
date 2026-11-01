"""Пресет «Нужен ответ» переименован в «Ответить»

Доска называется действием, которого просит каждая её карточка. Переименование трогает
только записи, где имя ещё равно старому значению по умолчанию: имя, которое пользователь
задал сам в настройках, менять нельзя — на то оно и своё.

Revision ID: d4f9e2b7a1c6
Revises: c8e1f4a7d2b9
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d4f9e2b7a1c6"
down_revision = "c8e1f4a7d2b9"
branch_labels = None
depends_on = None

OLD = "Нужен ответ"
NEW = "Ответить"


def _rename(old: str, new: str) -> None:
    specs = sa.table(
        "board_specs",
        sa.column("key", sa.String),
        sa.column("name", sa.String),
    )
    op.execute(
        specs.update()
        .where(specs.c.key == op.inline_literal("needreply"))
        .where(specs.c.name == op.inline_literal(old))
        .values(name=new)
    )


def upgrade() -> None:
    _rename(OLD, NEW)


def downgrade() -> None:
    _rename(NEW, OLD)
