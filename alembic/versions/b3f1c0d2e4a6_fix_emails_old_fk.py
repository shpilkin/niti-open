"""Fix dangling emails_old FK references left by account_scoping.

The account_scoping migration renamed ``emails`` -> ``emails_old`` to rebuild it. With
SQLite's modern ALTER TABLE (``legacy_alter_table`` OFF, the default since 3.25), that
rename also rewrote the foreign keys of child tables (``email_occurrences``,
``action_items``) to point at ``emails_old`` — which was then dropped, leaving the FKs
dangling. With ``PRAGMA foreign_keys=ON`` any INSERT into those tables then fails with
"no such table: main.emails_old" (seen during INBOX sync inserting email_occurrences).

This rebuilds each affected child table with its FK pointing back at ``emails``, keeping
the exact existing schema (only the FK target is corrected) and all rows. Idempotent:
tables that don't reference emails_old are left untouched, so it's a no-op on healthy DBs.

Revision ID: b3f1c0d2e4a6
Revises: a2d4f6b8c1e0
Create Date: 2026-07-01 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b3f1c0d2e4a6"
down_revision: str | None = "a2d4f6b8c1e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables whose FK to `emails` got rewritten to `emails_old` by the rename.
_CHILD_TABLES = ("email_occurrences", "action_items")


def upgrade() -> None:
    conn = op.get_bind()
    # legacy mode: our rename-back must NOT rewrite references in any other table
    # (that's the very bug we're fixing).
    op.execute("PRAGMA legacy_alter_table=ON")
    try:
        for name in _CHILD_TABLES:
            create_sql = conn.exec_driver_sql(
                f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{name}'"
            ).scalar()
            if not create_sql or "emails_old" not in create_sql:
                continue  # absent or already correct
            index_sqls = [
                r[0]
                for r in conn.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='index' "
                    f"AND tbl_name='{name}' AND sql IS NOT NULL"
                ).fetchall()
            ]
            # Correct only the FK target; keep the schema otherwise byte-for-byte.
            fixed = create_sql.replace('"emails_old"', '"emails"').replace(
                "emails_old", "emails"
            )
            tmp = f"_fixfk_{name}"
            op.execute(fixed.replace(f"CREATE TABLE {name}", f'CREATE TABLE "{tmp}"', 1))
            op.execute(f'INSERT INTO "{tmp}" SELECT * FROM {name}')
            op.execute(f"DROP TABLE {name}")
            op.execute(f'ALTER TABLE "{tmp}" RENAME TO {name}')
            for isql in index_sqls:  # rebuild dropped explicit indexes (PK/UNIQUE auto-rebuild)
                op.execute(isql)
    finally:
        op.execute("PRAGMA legacy_alter_table=OFF")


def downgrade() -> None:
    # Pure data/schema repair — nothing meaningful to revert.
    pass
