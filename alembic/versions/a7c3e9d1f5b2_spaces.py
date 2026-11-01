"""spaces: the context boundary («Работа» / «Личное») above mailboxes

docs/spaces.md. One space per existing mailbox, named after it, so nothing changes until
the user merges them in Настройки → Пространства. What moves:

* accounts, projects, action_items, meetings get a NOT NULL space_id;
* the per-mailbox Telegram bot (accounts.telegram_*) becomes a telegram_bots row;
* the per-mailbox model (accounts.model_connection_id), prompts and profile move to the
  space; signatures stay per mailbox — they are the sender's, not the context's;
* meetings.account_id (the mailbox that owned meetings) becomes meetings.space_id.

Ids are stored as 32-char hex (sa.Uuid on SQLite); app_settings keys/values carry the
dashed form — both spellings appear below on purpose.

Revision ID: a7c3e9d1f5b2
Revises: 5e2f7a9c1b3d
Create Date: 2026-09-05
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes
from alembic import op

revision: str = "a7c3e9d1f5b2"
down_revision: str | None = "5e2f7a9c1b3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_NAME = "Основное"


def _dashed(hex_id: str) -> str:
    return str(uuid.UUID(hex_id))


def _hex(dashed: str) -> str | None:
    try:
        return uuid.UUID(dashed).hex
    except (ValueError, AttributeError, TypeError):
        return None


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "spaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.Column("model_connection_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", email_reader.dbtypes.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["model_connection_id"], ["model_connections.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "telegram_bots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token_enc", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", email_reader.dbtypes.UTCDateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True,
        ),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_telegram_bots_space_id", "telegram_bots", ["space_id"])

    # --- one space per mailbox --------------------------------------------------------
    accounts = bind.execute(
        sa.text(
            "SELECT id, name, model_connection_id, telegram_token_enc, telegram_chat_id "
            "FROM accounts ORDER BY created_at, name"
        )
    ).fetchall()
    space_of: dict[str, str] = {}
    for n, (aid, name, mcid, token, chat) in enumerate(accounts):
        sid = uuid.uuid4().hex
        bind.execute(
            sa.text(
                "INSERT INTO spaces (id, name, sort, model_connection_id) "
                "VALUES (:id, :name, :sort, :mc)"
            ),
            {"id": sid, "name": name, "sort": n, "mc": mcid},
        )
        space_of[aid] = sid
        if token:
            bind.execute(
                sa.text(
                    "INSERT INTO telegram_bots (id, space_id, name, token_enc, chat_id, enabled) "
                    "VALUES (:id, :sid, :name, :tok, :chat, 1)"
                ),
                {"id": uuid.uuid4().hex, "sid": sid, "name": name, "tok": token, "chat": chat},
            )
    first: str | None = next(iter(space_of.values()), None)
    if first is None and any(
        bind.execute(sa.text(f"SELECT 1 FROM {t} LIMIT 1")).first()
        for t in ("meetings", "projects", "action_items")
    ):
        # No mailboxes, but rows that need a home: one space for them. An empty database
        # gets none — spaces.ensure_default creates it when the first source arrives.
        first = uuid.uuid4().hex
        bind.execute(
            sa.text("INSERT INTO spaces (id, name, sort) VALUES (:id, :name, 0)"),
            {"id": first, "name": DEFAULT_NAME},
        )

    # The space that owns meetings: the mailbox configured for them, else the only
    # mailbox, else the first space (same rule meetings.routing_account_id applied).
    raw = bind.execute(
        sa.text("SELECT value FROM app_settings WHERE key = 'meetings.account_id'")
    ).scalar()
    meetings_space = space_of.get(_hex(raw) or "", None)
    if meetings_space is None:
        meetings_space = next(iter(space_of.values())) if len(space_of) == 1 else first

    # --- accounts ---------------------------------------------------------------------
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("space_id", sa.Uuid(), nullable=True))
    for aid, sid in space_of.items():
        bind.execute(
            sa.text("UPDATE accounts SET space_id = :sid WHERE id = :aid"),
            {"sid": sid, "aid": aid},
        )
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.alter_column("space_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_foreign_key("fk_accounts_space", "spaces", ["space_id"], ["id"])
        batch_op.create_index("ix_accounts_space_id", ["space_id"], unique=False)
        batch_op.drop_column("telegram_chat_id")
        batch_op.drop_column("telegram_token_enc")
        batch_op.drop_column("model_connection_id")

    # --- meetings ---------------------------------------------------------------------
    with op.batch_alter_table("meetings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("space_id", sa.Uuid(), nullable=True))
    bind.execute(sa.text("UPDATE meetings SET space_id = :sid"), {"sid": meetings_space})
    with op.batch_alter_table("meetings", schema=None) as batch_op:
        batch_op.alter_column("space_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_foreign_key("fk_meetings_space", "spaces", ["space_id"], ["id"])
        batch_op.create_index("ix_meetings_space_id", ["space_id"], unique=False)

    # --- projects ---------------------------------------------------------------------
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("space_id", sa.Uuid(), nullable=True))
    bind.execute(
        sa.text(
            "UPDATE projects SET space_id = "
            "(SELECT a.space_id FROM accounts a WHERE a.id = projects.account_id)"
        )
    )
    # A project without a mailbox was a meetings-only project: it goes where meetings go.
    bind.execute(
        sa.text("UPDATE projects SET space_id = :sid WHERE space_id IS NULL"),
        {"sid": meetings_space},
    )
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.alter_column("space_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_foreign_key("fk_projects_space", "spaces", ["space_id"], ["id"])
        batch_op.create_index("ix_projects_space_id", ["space_id"], unique=False)
        batch_op.drop_index("ix_projects_account_id")
        batch_op.drop_column("account_id")

    # --- action items -----------------------------------------------------------------
    with op.batch_alter_table("action_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("space_id", sa.Uuid(), nullable=True))
    for stmt in (
        # Owned directly (Telegram, quick-add).
        "UPDATE action_items SET space_id = "
        "(SELECT a.space_id FROM accounts a WHERE a.id = action_items.account_id) "
        "WHERE account_id IS NOT NULL",
        # Through the thread's mailbox.
        "UPDATE action_items SET space_id = "
        "(SELECT a.space_id FROM threads t JOIN accounts a ON a.id = t.account_id "
        " WHERE t.id = action_items.source_thread_id) "
        "WHERE space_id IS NULL AND source_thread_id IS NOT NULL",
        # A thread whose mailbox was detached: any occurrence of its mail still knows.
        "UPDATE action_items SET space_id = "
        "(SELECT a.space_id FROM emails e "
        " JOIN email_occurrences o ON o.email_id = e.id "
        " JOIN accounts a ON a.id = o.account_id "
        " WHERE e.thread_id = action_items.source_thread_id LIMIT 1) "
        "WHERE space_id IS NULL AND source_thread_id IS NOT NULL",
        # Calendar-sourced (the archive still holds prep items).
        "UPDATE action_items SET space_id = "
        "(SELECT a.space_id FROM calendar_events ce JOIN accounts a ON a.id = ce.account_id "
        " WHERE ce.id = action_items.source_event_id) "
        "WHERE space_id IS NULL AND source_event_id IS NOT NULL",
        # Spoken at a meeting.
        "UPDATE action_items SET space_id = "
        "(SELECT m.space_id FROM meetings m WHERE m.id = action_items.source_meeting_id) "
        "WHERE space_id IS NULL AND source_meeting_id IS NOT NULL",
    ):
        bind.execute(sa.text(stmt))
    bind.execute(
        sa.text("UPDATE action_items SET space_id = :sid WHERE space_id IS NULL"),
        {"sid": meetings_space},
    )
    with op.batch_alter_table("action_items", schema=None) as batch_op:
        batch_op.alter_column("space_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_foreign_key(
            "fk_action_items_space", "spaces", ["space_id"], ["id"]
        )
        batch_op.create_index("ix_action_items_space_id", ["space_id"], unique=False)
        batch_op.drop_index("ix_action_items_account_id")
        batch_op.drop_constraint("fk_action_items_account", type_="foreignkey")
        batch_op.drop_column("account_id")

    # --- settings keyed by mailbox -> by space ----------------------------------------
    for aid, sid in space_of.items():
        old, new = _dashed(aid), _dashed(sid)
        bind.execute(
            sa.text(
                "UPDATE app_settings SET key = replace(key, :old, :new) "
                "WHERE (key LIKE 'prompt:%' OR key LIKE 'profile:%') AND key LIKE :pat"
            ),
            {"old": old, "new": new, "pat": f"%{old}"},
        )
    bind.execute(sa.text("DELETE FROM app_settings WHERE key = 'meetings.account_id'"))
    if meetings_space is not None:
        bind.execute(
            sa.text(
                "INSERT INTO app_settings (key, value, is_secret) "
                "VALUES ('meetings.space_id', :v, 0)"
            ),
            {"v": _dashed(meetings_space)},
        )


def downgrade() -> None:
    """Best effort: the columns come back, the assignments do not (a space is not a
    mailbox — there is nothing to map a merged space back to)."""
    bind = op.get_bind()
    with op.batch_alter_table("action_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("account_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_action_items_account", "accounts", ["account_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.create_index("ix_action_items_account_id", ["account_id"], unique=False)
        batch_op.drop_index("ix_action_items_space_id")
        batch_op.drop_constraint("fk_action_items_space", type_="foreignkey")
        batch_op.drop_column("space_id")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("account_id", sa.Uuid(), nullable=True))
        batch_op.create_index("ix_projects_account_id", ["account_id"], unique=False)
        batch_op.drop_index("ix_projects_space_id")
        batch_op.drop_constraint("fk_projects_space", type_="foreignkey")
        batch_op.drop_column("space_id")
    with op.batch_alter_table("meetings", schema=None) as batch_op:
        batch_op.drop_index("ix_meetings_space_id")
        batch_op.drop_constraint("fk_meetings_space", type_="foreignkey")
        batch_op.drop_column("space_id")
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("model_connection_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("telegram_token_enc", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True))
        batch_op.drop_index("ix_accounts_space_id")
        batch_op.drop_constraint("fk_accounts_space", type_="foreignkey")
        batch_op.drop_column("space_id")
    bind.execute(sa.text("DELETE FROM app_settings WHERE key = 'meetings.space_id'"))
    op.drop_index("ix_telegram_bots_space_id", table_name="telegram_bots")
    op.drop_table("telegram_bots")
    op.drop_table("spaces")
