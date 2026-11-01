"""Account scoping: per-mailbox isolation + per-mailbox model

Adds account_id to emails/threads/projects and model_connection_id to accounts.
Rebuilds `emails` to drop the GLOBAL unique on rfc_message_id (dedup is now per-account)
and add a composite unique (account_id, rfc_message_id); the FTS triggers (dropped with
the table) are recreated afterwards. Existing rows are backfilled from occurrences /
the single configured account.

Revision ID: a2d4f6b8c1e0
Revises: f1c8d2a6b5e0
Create Date: 2026-06-30 17:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.schema import CreateIndex, CreateTable

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = 'a2d4f6b8c1e0'
down_revision: str | None = 'f1c8d2a6b5e0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FTS_TRIGGERS = [
    "CREATE TRIGGER emails_fts_ai AFTER INSERT ON emails BEGIN "
    "INSERT INTO emails_fts(email_id, subject, body, sender) VALUES ("
    "new.id, coalesce(new.subject,''), coalesce(new.body_text,''), "
    "coalesce(new.from_name,'')||' '||coalesce(new.from_addr,'')); END",
    "CREATE TRIGGER emails_fts_ad AFTER DELETE ON emails BEGIN "
    "DELETE FROM emails_fts WHERE email_id = old.id; END",
    "CREATE TRIGGER emails_fts_au AFTER UPDATE ON emails BEGIN "
    "DELETE FROM emails_fts WHERE email_id = old.id; "
    "INSERT INTO emails_fts(email_id, subject, body, sender) VALUES ("
    "new.id, coalesce(new.subject,''), coalesce(new.body_text,''), "
    "coalesce(new.from_name,'')||' '||coalesce(new.from_addr,'')); END",
]


def upgrade() -> None:
    conn = op.get_bind()

    # --- simple nullable FK columns (ALTER ADD COLUMN; no table rebuild) ---
    op.execute("ALTER TABLE threads ADD COLUMN account_id CHAR(32) REFERENCES accounts(id)")
    op.execute("CREATE INDEX ix_threads_account_id ON threads (account_id)")
    op.execute("ALTER TABLE projects ADD COLUMN account_id CHAR(32) REFERENCES accounts(id)")
    op.execute("CREATE INDEX ix_projects_account_id ON projects (account_id)")
    op.execute(
        "ALTER TABLE accounts ADD COLUMN model_connection_id CHAR(32) "
        "REFERENCES model_connections(id) ON DELETE SET NULL"
    )

    # --- rebuild `emails` (drop global unique on rfc_message_id, add account_id) ---
    # Import the model here so the new table matches the ORM exactly (single source of
    # truth). FK enforcement is OFF during migrations, so the copy is safe.
    from email_reader.models import Email

    for trg in ("emails_fts_ai", "emails_fts_ad", "emails_fts_au"):
        op.execute(f"DROP TRIGGER IF EXISTS {trg}")

    old_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(emails)").fetchall()]
    new_cols = [c.name for c in Email.__table__.columns]
    common = [c for c in old_cols if c in new_cols and c != "account_id"]
    collist = ", ".join(common)

    # legacy_alter_table=ON so RENAME does NOT rewrite child tables' FKs (email_occurrences,
    # action_items) to point at emails_old — that rewrite (modern SQLite default) would leave
    # them dangling once emails_old is dropped. See migration b3f1c0d2e4a6, which repairs DBs
    # already corrupted by the previous version of this migration.
    op.execute("PRAGMA legacy_alter_table=ON")
    op.execute("ALTER TABLE emails RENAME TO emails_old")
    op.execute(str(CreateTable(Email.__table__).compile(dialect=conn.dialect)))
    op.execute(
        f"INSERT INTO emails (account_id, {collist}) "
        f"SELECT (SELECT eo.account_id FROM email_occurrences eo "
        f"        WHERE eo.email_id = emails_old.id LIMIT 1), {collist} "
        f"FROM emails_old"
    )
    # Drop the old table first: a renamed table keeps its index NAMES, so the new
    # indexes (same names) must be created only after emails_old (and its indexes) is gone.
    op.execute("DROP TABLE emails_old")
    op.execute("PRAGMA legacy_alter_table=OFF")
    for ix in Email.__table__.indexes:
        op.execute(str(CreateIndex(ix).compile(dialect=conn.dialect)))
    for stmt in _FTS_TRIGGERS:
        op.execute(stmt)

    # --- backfill account_id ---
    # Emails with no occurrence (e.g. locally-composed sent copies) + threads/projects:
    # fall back to the single configured account when there is exactly one.
    single = conn.exec_driver_sql("SELECT id FROM accounts LIMIT 2").fetchall()
    if len(single) == 1:
        acc = single[0][0]
        op.execute(f"UPDATE emails SET account_id = '{acc}' WHERE account_id IS NULL")
        op.execute(f"UPDATE projects SET account_id = '{acc}' WHERE account_id IS NULL")
    op.execute(
        "UPDATE threads SET account_id = ("
        "SELECT e.account_id FROM emails e "
        "WHERE e.thread_id = threads.id AND e.account_id IS NOT NULL LIMIT 1) "
        "WHERE account_id IS NULL"
    )


def downgrade() -> None:
    # One-way: rebuilding `emails` back with a global unique is unsafe once per-account
    # duplicates exist. Drop the added columns only.
    op.execute("DROP INDEX IF EXISTS ix_threads_account_id")
    op.execute("DROP INDEX IF EXISTS ix_projects_account_id")
    with op.batch_alter_table("threads") as b:
        b.drop_column("account_id")
    with op.batch_alter_table("projects") as b:
        b.drop_column("account_id")
    with op.batch_alter_table("accounts") as b:
        b.drop_column("model_connection_id")
