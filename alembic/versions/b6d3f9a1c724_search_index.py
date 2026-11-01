"""cross-cutting search index (search_docs + search_index)

Revision ID: b6d3f9a1c724
Revises: a5c1e8f3b7d2
Create Date: 2026-09-06 10:00:00.000000

The tables are created empty on purpose. Filling them means stemming every mail body,
which is Python work — seconds on a big mailbox — and that belongs in the worker's
first idle sweep (`pipeline.worker`), not in a migration running while the user waits for
the app to open. Search finds nothing until that sweep; it is one sweep, not a state to
design around.

`emails_fts` goes at the same time. It only ever covered mail, only subject/body/sender,
and had no reader left once `/search` moved to the new index — keeping it would mean
three triggers writing 11 MB nobody queries.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from email_reader.search.index import DDL

revision: str = "b6d3f9a1c724"
down_revision: str | None = "a5c1e8f3b7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for stmt in DDL:
        op.execute(stmt)
    # The old mail-only index (f1c8d2a6b5e0) and its triggers: superseded by search_index.
    for trigger in ("emails_fts_ai", "emails_fts_ad", "emails_fts_au"):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute("DROP TABLE IF EXISTS emails_fts")


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute("DROP TABLE IF EXISTS search_index")
    op.execute("DROP TABLE IF EXISTS search_docs")
    # Downgrade rebuilds the old index from the store so mail search keeps working.
    op.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5("
        "email_id UNINDEXED, subject, body, sender, "
        "tokenize='unicode61 remove_diacritics 2')"
    )
    op.execute(
        "INSERT INTO emails_fts(email_id, subject, body, sender) "
        "SELECT id, coalesce(subject,''), coalesce(body_text,''), "
        "coalesce(from_name,'')||' '||coalesce(from_addr,'') FROM emails"
    )
    insert = (
        "INSERT INTO emails_fts(email_id, subject, body, sender) VALUES ("
        "new.id, coalesce(new.subject,''), coalesce(new.body_text,''), "
        "coalesce(new.from_name,'')||' '||coalesce(new.from_addr,''))"
    )
    op.execute(f"CREATE TRIGGER emails_fts_ai AFTER INSERT ON emails BEGIN {insert}; END")
    op.execute("CREATE TRIGGER emails_fts_ad AFTER DELETE ON emails BEGIN "
               "DELETE FROM emails_fts WHERE email_id = old.id; END")
    op.execute("CREATE TRIGGER emails_fts_au AFTER UPDATE OF subject, body_text, "
               "from_name, from_addr ON emails BEGIN "
               "DELETE FROM emails_fts WHERE email_id = old.id; " + insert + "; END")
