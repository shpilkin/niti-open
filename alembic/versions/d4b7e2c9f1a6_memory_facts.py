"""memory_facts: the space profile and the notes on a person become rows

The owner's memory used to be two free texts — `app_settings['profile:<space>']` and
`people.notes` — that grew by appending and could only be edited whole. Each becomes
rows in `memory_facts` (docs/feature-memory-facts.md §3): the profile as one fact holding
the editor's text, each line of a person's notes as one fact in line order. They render
back into exactly the text the prompts got before, so nothing downstream changes; the
old column and keys go in the same step — two stores for one thing would be a second
source of truth.

Blank lines inside a person's notes do not survive the split (a fact is never empty); the
prompts never saw them either — memory/context collapses whitespace before rendering.

Revision ID: d4b7e2c9f1a6
Revises: b6e2a9f4c7d3
Create Date: 2026-09-12
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

import email_reader.dbtypes  # noqa: F401  (custom column types in migrations)
from alembic import op

revision: str = "d4b7e2c9f1a6"
down_revision: str | None = "b6e2a9f4c7d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAMP = email_reader.dbtypes.UTCDateTime


def _facts():
    return sa.table(
        "memory_facts",
        sa.column("id", sa.Uuid()), sa.column("space_id", sa.Uuid()),
        sa.column("subject_kind", sa.String()), sa.column("subject_key", sa.String()),
        sa.column("kind", sa.String()), sa.column("text", sa.Text()),
        sa.column("evidence_kind", sa.String()), sa.column("evidence_id", sa.String()),
        sa.column("status", sa.String()), sa.column("valid_from", _STAMP()),
        sa.column("valid_to", _STAMP()), sa.column("supersedes_id", sa.Uuid()),
        sa.column("seq", sa.Integer()), sa.column("created_at", _STAMP()),
    )


def _row(now: datetime, **values) -> dict:
    return {
        "id": uuid.uuid4(), "space_id": None, "subject_key": None, "kind": "fact",
        "evidence_id": None, "status": "active", "valid_from": now, "valid_to": None,
        "supersedes_id": None, "created_at": now, **values,
    }


def upgrade() -> None:
    op.create_table(
        "memory_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=True),
        sa.Column("subject_kind", sa.String(), nullable=False),
        sa.Column("subject_key", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("evidence_kind", sa.String(), nullable=False),
        sa.Column("evidence_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("valid_from", _STAMP(timezone=True), nullable=True),
        sa.Column("valid_to", _STAMP(timezone=True), nullable=True),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("created_at", _STAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_facts_subject", "memory_facts",
        ["space_id", "subject_kind", "subject_key", "status"], unique=False,
    )
    op.create_index(
        "ix_memory_facts_evidence", "memory_facts", ["evidence_kind", "evidence_id"],
        unique=False,
    )

    conn = op.get_bind()
    now = datetime.now(UTC)
    rows: list[dict] = []
    for pid, notes in conn.execute(
        sa.text("SELECT id, notes FROM people WHERE notes IS NOT NULL AND notes <> ''")
    ):
        key = uuid.UUID(str(pid)).hex
        lines = [line.rstrip() for line in str(notes).splitlines() if line.strip()]
        for i, line in enumerate(lines, start=1):
            rows.append(_row(now, subject_kind="person", subject_key=key, text=line,
                             evidence_kind="notes", seq=i))
    spaces = {
        uuid.UUID(str(sid)) for (sid,) in conn.execute(sa.text("SELECT id FROM spaces"))
    }
    for key, value in conn.execute(
        sa.text("SELECT key, value FROM app_settings "
                "WHERE key LIKE 'profile:%' AND value IS NOT NULL AND value <> ''")
    ):
        try:
            space_id = uuid.UUID(str(key).split(":", 1)[1])
        except ValueError:
            continue
        if space_id not in spaces:
            continue  # a profile of a space that no longer exists has no owner to keep it
        rows.append(_row(now, space_id=space_id, subject_kind="me", text=str(value).strip(),
                         evidence_kind="profile", seq=0))
    if rows:
        conn.execute(_facts().insert(), rows)

    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.drop_column("notes")
    op.execute("DELETE FROM app_settings WHERE key LIKE 'profile:%'")


def _line(subject_kind: str, evidence_kind: str, text: str, valid_from) -> str:
    # Mirrors memory.facts.line_of: a remembered line carries its date.
    if evidence_kind == "remember" and valid_from:
        stamp = f"{str(valid_from)[:10]}: {text}"
        return f"- {stamp}" if subject_kind == "me" else stamp
    return text


def downgrade() -> None:
    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.add_column(sa.Column("notes", sa.Text(), nullable=True))
    conn = op.get_bind()
    texts: dict[tuple[str, str | None, str | None], list[str]] = {}
    for kind, space_id, key, text, ev, valid_from in conn.execute(sa.text(
        "SELECT subject_kind, space_id, subject_key, text, evidence_kind, valid_from "
        "FROM memory_facts WHERE status = 'active' AND valid_to IS NULL "
        "ORDER BY seq, created_at"
    )):
        texts.setdefault((kind, str(space_id) if space_id else None, key), []).append(
            _line(kind, ev, text, valid_from)
        )
    for (kind, space_id, key), lines in texts.items():
        value = "\n".join(lines)
        if kind == "person" and key:
            conn.execute(sa.text("UPDATE people SET notes = :n WHERE id = :id"),
                         {"n": value, "id": uuid.UUID(hex=key).hex})
        elif kind == "me" and space_id:
            conn.execute(
                sa.text("INSERT INTO app_settings (key, value, is_secret) "
                        "VALUES (:k, :v, 0)"),
                {"k": f"profile:{uuid.UUID(str(space_id))}", "v": value},
            )
    op.drop_index("ix_memory_facts_evidence", table_name="memory_facts")
    op.drop_index("ix_memory_facts_subject", table_name="memory_facts")
    op.drop_table("memory_facts")
