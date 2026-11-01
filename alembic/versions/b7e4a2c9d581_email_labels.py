"""email_labels — метка на письме, а не на беседе

Revision ID: b7e4a2c9d581
Revises: a3d7f1c95b28
Create Date: 2026-09-10 12:00:00.000000

Правило заводят на адрес, а метка вешалась на всю беседу: одно письмо от нужного
человека красило переписку целиком, и метка появлялась на письмах других людей.

Метки не переносятся из `thread_labels` — это сохранило бы ту же ошибку. Они
пересчитываются по правилам, письмо за письмом.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e4a2c9d581"
down_revision: str | None = "a3d7f1c95b28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _addrs(direction: str, from_addr: str | None, to_raw, cc_raw) -> set[str]:
    """Адреса второй стороны — как в labels.correspondents()."""
    if direction == "inbound":
        return {(from_addr or "").strip().lower()}
    out: set[str] = set()
    for raw in (to_raw, cc_raw):
        try:
            items = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except (TypeError, ValueError):
            items = []
        for a in items or []:
            addr = (a or {}).get("email") if isinstance(a, dict) else None
            if addr:
                out.add(addr.strip().lower())
    return out


def upgrade() -> None:
    op.create_table(
        "email_labels",
        sa.Column("email_id", sa.Uuid(), nullable=False),
        sa.Column("label_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["email_id"], ["emails.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["label_id"], ["labels.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("email_id", "label_id"),
    )
    conn = op.get_bind()
    rules = conn.execute(sa.text(
        "select match_type, pattern, label_id from sender_rules "
        "where action = 'label' and label_id is not null"
    )).all()
    if not rules:
        op.drop_table("thread_labels")
        return
    by_addr: dict[str, set] = {}
    by_domain: dict[str, set] = {}
    for match_type, pattern, label_id in rules:
        target = by_addr if match_type == "address" else by_domain
        target.setdefault((pattern or "").strip().lower(), set()).add(label_id)

    rows = conn.execute(sa.text(
        "select id, direction, from_addr, to_addrs, cc_addrs from emails"
    ))
    pairs: list[dict] = []
    for email_id, direction, from_addr, to_raw, cc_raw in rows:
        addrs = {a for a in _addrs(direction, from_addr, to_raw, cc_raw) if a}
        if not addrs:
            continue
        labels: set = set()
        for addr in addrs:
            labels |= by_addr.get(addr, set())
            if "@" in addr:
                labels |= by_domain.get(addr.split("@", 1)[1], set())
        pairs.extend({"e": email_id, "l": lid} for lid in labels)
    if pairs:
        conn.execute(
            sa.text("insert into email_labels (email_id, label_id) values (:e, :l)"),
            pairs,
        )
    op.drop_table("thread_labels")


def downgrade() -> None:
    op.create_table(
        "thread_labels",
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("label_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["label_id"], ["labels.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("thread_id", "label_id"),
    )
    op.get_bind().execute(sa.text(
        "insert or ignore into thread_labels (thread_id, label_id) "
        "select e.thread_id, el.label_id from email_labels el "
        "join emails e on e.id = el.email_id where e.thread_id is not null"
    ))
    op.drop_table("email_labels")
