"""people: identities (address or name) instead of one address per person

A person reaches Niti under several names — an address in the mail, a name read off a
transcript, a name typed into a task. Each becomes a `person_identities` row pointing at
the profile, so several of them can be merged into one human being without touching
anything else. `people.email` stays as the primary address (person_stats and sender_rules
are address-keyed) but becomes nullable: a profile known only by name has none.

Existing rows are backfilled one identity each, so nothing that was remembered is lost.

Revision ID: e7b3d1f8a2c5
Revises: c9e5a3b7d2f4
Create Date: 2026-09-05
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = "e7b3d1f8a2c5"
down_revision: str | None = "c9e5a3b7d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "person_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("display", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column(
            "created_at", email_reader.dbtypes.UTCDateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "value", name="uq_person_identity"),
    )
    op.create_index(
        "ix_person_identities_person_id", "person_identities", ["person_id"], unique=False
    )

    # Every existing profile keeps its address — as its identity row now.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, email, name FROM people WHERE email IS NOT NULL AND email <> ''")
    ).fetchall()
    for pid, email, name in rows:
        conn.execute(
            sa.text(
                "INSERT INTO person_identities (id, person_id, kind, value, display, source)"
                " VALUES (:id, :pid, 'email', :value, :display, 'mail')"
            ),
            {"id": str(uuid.uuid4()), "pid": pid, "value": (email or "").strip().lower(),
             "display": name or email},
        )

    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.alter_column("email", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    # A name-only profile has no address to put back; it goes with the table.
    op.execute("DELETE FROM people WHERE email IS NULL OR email = ''")
    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.alter_column("email", existing_type=sa.String(), nullable=False)
    op.drop_index("ix_person_identities_person_id", table_name="person_identities")
    op.drop_table("person_identities")
