"""board_specs — доски пространства настраиваются, а не зашиты в сайдбар

Доска это запрос по индексированным колонкам (типы, статусы, условие, сортировка), поэтому
её параметры переносятся в базу: пользователь прячет ненужные доски, переименовывает их и
собирает свои. Пресеты заводятся в каждом пространстве с ключом, равным прежнему ключу
доски, — старые ссылки /view/today, cookie группировки и знание бота о досках остаются в силе.

Revision ID: b6e2a9f4c7d3
Revises: a3d7f1c9e6b4
Create Date: 2026-09-12
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = "b6e2a9f4c7d3"
down_revision: str | None = "a3d7f1c9e6b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Копия boards.PRESETS на момент миграции: (ключ, имя, иконка, типы). Копия, а не импорт —
# миграция должна пережить переименование пресета в коде.
_PRESETS = (
    ("today", "Сегодня", "sun", []),
    ("triage", "Разобрать", "inbox", []),
    ("needreply", "Нужен ответ", "mail", ["NEED_MY_REPLY"]),
    ("waiting", "Жду ответа", "clock", ["WAITING_FOR_REPLY"]),
    ("delegated", "Делегировано", "forward", ["TO_DELEGATE", "DELEGATED_TASK"]),
    ("commitments", "Мои задачи", "check", ["MY_COMMITMENT", "TASK"]),
    ("snoozed", "Отложенные", "clock", []),
    ("done", "Архив", "archive", []),
)


def upgrade() -> None:
    op.create_table(
        "board_specs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("icon", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "hidden", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "builtin", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("types", sa.JSON(), nullable=False),
        sa.Column("condition", sa.String(), nullable=True),
        sa.Column("sort", sa.String(), nullable=True),
        sa.Column(
            "created_at", email_reader.dbtypes.UTCDateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("space_id", "key", name="uq_board_space_key"),
    )
    op.create_index("ix_board_specs_space_id", "board_specs", ["space_id"], unique=False)

    conn = op.get_bind()
    spaces = conn.execute(sa.text("SELECT id FROM spaces")).fetchall()
    for (space_id,) in spaces:
        for n, (key, name, icon, types) in enumerate(_PRESETS):
            conn.execute(
                sa.text(
                    "INSERT INTO board_specs"
                    " (id, space_id, key, kind, name, icon, position, hidden, builtin,"
                    "  types, condition, sort)"
                    " VALUES (:id, :space, :key, :key2, :name, :icon, :pos, 0, 1,"
                    "         :types, '', 'due_asc')"
                ),
                {"id": str(uuid.uuid4()), "space": space_id, "key": key, "key2": key,
                 "name": name, "icon": icon, "pos": n, "types": json.dumps(types)},
            )


def downgrade() -> None:
    op.drop_index("ix_board_specs_space_id", table_name="board_specs")
    op.drop_table("board_specs")
