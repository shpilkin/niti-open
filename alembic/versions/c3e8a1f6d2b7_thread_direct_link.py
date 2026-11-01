"""Нить: прямая привязка карточки, имя уникально в пространстве, «отложена до»

Карточка без беседы и встречи (Telegram, вручную) не может унаследовать Нить от
источника — у неё появляется прямая ссылка `action_items.project_id`; она же сильнее
принадлежности через источник, когда владелец или агент указал Нить явно
(docs/feature-topics.md §8.3, §8.6). Имя Нити было уникально глобально — на втором
пространстве это упиралось; теперь уникально в пространстве. `snoozed_until` — одна дата
на Нить вместо N отложенных карточек (§8.4). `status` — `active` или `proposed`: Нить,
которую предложил агент через MCP, ждёт «Да» владельца и до него не участвует ни в
маршрутизации, ни в списках (§9.1); `proposal` — довод и улики предложения;
`agent_note` — заметка агента, в промпты не попадает (§9.1 п. 2).

Revision ID: c3e8a1f6d2b7
Revises: a1c7f4e9b2d3
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c3e8a1f6d2b7"
down_revision = "a1c7f4e9b2d3"
branch_labels = None
depends_on = None

# Встроенное UNIQUE(name) у projects безымянное; в batch-режиме SQLite его можно снять
# только под именем, которое даёт соглашение (alembic, «Dropping Unnamed or Named
# Foreign Key Constraints» — для unique то же самое).
_NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    with op.batch_alter_table("action_items", schema=None) as batch:
        batch.add_column(sa.Column("project_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_action_items_project", "projects", ["project_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_action_items_project_id", ["project_id"], unique=False)
    with op.batch_alter_table("projects", schema=None, naming_convention=_NAMING) as batch:
        batch.drop_constraint("uq_projects_name", type_="unique")
        batch.create_unique_constraint("uq_projects_space_name", ["space_id", "name"])
        batch.add_column(sa.Column("snoozed_until", sa.Date(), nullable=True))
        batch.add_column(sa.Column(
            "status", sa.String(), nullable=False, server_default="active"
        ))
        batch.add_column(sa.Column("proposal", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("agent_note", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch:
        batch.drop_column("agent_note")
        batch.drop_column("proposal")
        batch.drop_column("status")
        batch.drop_column("snoozed_until")
        batch.drop_constraint("uq_projects_space_name", type_="unique")
        batch.create_unique_constraint("uq_projects_name", ["name"])
    with op.batch_alter_table("action_items", schema=None) as batch:
        batch.drop_index("ix_action_items_project_id")
        batch.drop_constraint("fk_action_items_project", type_="foreignkey")
        batch.drop_column("project_id")
