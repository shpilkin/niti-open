"""full-text search over emails (FTS5)

Revision ID: f1c8d2a6b5e0
Revises: e7b2f1a9c3d4
Create Date: 2026-06-30 11:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'f1c8d2a6b5e0'
down_revision: str | None = 'e7b2f1a9c3d4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Standalone FTS5 index (email_id UNINDEXED links back to emails.id). Triggers keep
    # it in sync; diacritics-insensitive unicode tokenizer for RU/EN.
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
    op.execute(
        "CREATE TRIGGER emails_fts_ai AFTER INSERT ON emails BEGIN "
        "INSERT INTO emails_fts(email_id, subject, body, sender) VALUES ("
        "new.id, coalesce(new.subject,''), coalesce(new.body_text,''), "
        "coalesce(new.from_name,'')||' '||coalesce(new.from_addr,'')); END"
    )
    op.execute(
        "CREATE TRIGGER emails_fts_ad AFTER DELETE ON emails BEGIN "
        "DELETE FROM emails_fts WHERE email_id = old.id; END"
    )
    op.execute(
        "CREATE TRIGGER emails_fts_au AFTER UPDATE ON emails BEGIN "
        "DELETE FROM emails_fts WHERE email_id = old.id; "
        "INSERT INTO emails_fts(email_id, subject, body, sender) VALUES ("
        "new.id, coalesce(new.subject,''), coalesce(new.body_text,''), "
        "coalesce(new.from_name,'')||' '||coalesce(new.from_addr,'')); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS emails_fts_au")
    op.execute("DROP TRIGGER IF EXISTS emails_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS emails_fts_ai")
    op.execute("DROP TABLE IF EXISTS emails_fts")
