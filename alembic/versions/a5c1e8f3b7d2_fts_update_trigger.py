"""narrow the emails_fts update trigger to the indexed columns

Revision ID: a5c1e8f3b7d2
Revises: e7b3d1f8a2c5
Create Date: 2026-09-06 09:00:00.000000

The old trigger fired on ANY update of `emails` — including `read_at`, which the
\\Seen absorb writes in bulk on every sync — and re-wrote the whole document each
time. FTS5 keeps the superseded versions in its segments until a merge, so the
index grew many times larger than the text itself. Narrowing the trigger stops the growth;
`optimize` merges what is already there into the live pages. The freed
pages stay inside the file until a VACUUM (Settings -> Storage -> "Сжать базу").
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a5c1e8f3b7d2"
down_revision: str | None = "e7b3d1f8a2c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INSERT = (
    "INSERT INTO emails_fts(email_id, subject, body, sender) VALUES ("
    "new.id, coalesce(new.subject,''), coalesce(new.body_text,''), "
    "coalesce(new.from_name,'')||' '||coalesce(new.from_addr,''))"
)


def _has_fts(conn) -> bool:  # noqa: ANN001
    row = conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='emails_fts'"
    ).fetchone()
    return row is not None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "sqlite" or not _has_fts(conn):
        return
    op.execute("DROP TRIGGER IF EXISTS emails_fts_au")
    op.execute(
        "CREATE TRIGGER emails_fts_au AFTER UPDATE OF subject, body_text, from_name, "
        "from_addr ON emails BEGIN "
        "DELETE FROM emails_fts WHERE email_id = old.id; " + _INSERT + "; END"
    )
    op.execute("INSERT INTO emails_fts(emails_fts) VALUES('optimize')")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "sqlite" or not _has_fts(conn):
        return
    op.execute("DROP TRIGGER IF EXISTS emails_fts_au")
    op.execute(
        "CREATE TRIGGER emails_fts_au AFTER UPDATE ON emails BEGIN "
        "DELETE FROM emails_fts WHERE email_id = old.id; " + _INSERT + "; END"
    )
