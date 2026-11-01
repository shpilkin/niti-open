"""Labels + sender rules (Yandex-style).

A label is a colored tag on a MESSAGE. A sender rule maps a correspondent (exact
address or domain) to a label; rules are applied deterministically at ingest and via
"apply to existing". Correspondent = the other party: sender for inbound mail,
recipients for outbound.

Метка на письме, а не на беседе: правило заводят на адрес, и метку должны получать
письма именно этого человека. На беседе она красила всю переписку — и висела на
письмах тех, к кому правило не относится.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.enums import Direction
from email_reader.models import Email, EmailLabel, Label, SenderRule
from email_reader.normalization import normalize_addr


# ---- Labels CRUD ----
async def list_labels(session: AsyncSession) -> list[Label]:
    return list((await session.execute(select(Label).order_by(Label.name))).scalars().all())


async def create_label(session: AsyncSession, name: str, color: str) -> Label | None:
    name = name.strip()
    if not name:
        return None
    existing = (
        await session.execute(select(Label).where(Label.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        existing.color = color or existing.color
        return existing
    label = Label(name=name, color=color or "#9aa0a6")
    session.add(label)
    await session.flush()
    return label


async def delete_label(session: AsyncSession, label_id: uuid.UUID) -> None:
    await session.execute(delete(Label).where(Label.id == label_id))


# ---- Rules CRUD ----
RULE_ACTIONS = ("label", "ignore", "delegate")


async def list_rules(session: AsyncSession) -> list[tuple[SenderRule, Label | None]]:
    """Every rule with its label (None for the ignore/delegate kinds)."""
    rows = await session.execute(
        select(SenderRule, Label)
        .outerjoin(Label, SenderRule.label_id == Label.id)
        .order_by(SenderRule.created_at)
    )
    return [(r, lbl) for r, lbl in rows]


async def rules_for_address(session: AsyncSession, addr: str) -> list[SenderRule]:
    """Rules that fire for this address (by address or by its domain)."""
    rules = (await session.execute(select(SenderRule))).scalars().all()
    return matching_rules(list(rules), {normalize_addr(addr)})


def _normalize_pattern(match_type: str, pattern: str) -> str:
    p = (pattern or "").strip().lower().lstrip("@")
    return p


async def create_rule(
    session: AsyncSession, match_type: str, pattern: str, label_id: uuid.UUID | None = None,
    *, action: str = "label", value: str | None = None,
) -> SenderRule | None:
    """None when the rule would be meaningless: no pattern, a label rule without a label,
    a delegate rule without a delegate, or an exact duplicate (same pattern, action, value)."""
    if match_type not in ("address", "domain"):
        match_type = "address"
    if action not in RULE_ACTIONS:
        return None
    p = _normalize_pattern(match_type, pattern)
    value = (value or "").strip() or None
    if not p or (action == "label" and label_id is None) or (action == "delegate" and not value):
        return None
    dup = await session.execute(
        select(SenderRule).where(
            SenderRule.match_type == match_type, SenderRule.pattern == p,
            SenderRule.action == action, SenderRule.value == value,
            SenderRule.label_id == label_id,
        )
    )
    if dup.scalars().first() is not None:
        return None
    rule = SenderRule(
        match_type=match_type, pattern=p, action=action, value=value,
        label_id=label_id if action == "label" else None,
    )
    session.add(rule)
    await session.flush()
    return rule


async def delete_rule(session: AsyncSession, rule_id: uuid.UUID) -> None:
    await session.execute(delete(SenderRule).where(SenderRule.id == rule_id))


# ---- Matching / application ----
def correspondents(email: Email) -> set[str]:
    """The other party's addresses for this email."""
    if email.direction == Direction.INBOUND:
        return {normalize_addr(email.from_addr)}
    out = set()
    for a in (email.to_addrs or []) + (email.cc_addrs or []):
        e = a.get("email")
        if e:
            out.add(normalize_addr(e))
    return out


def matching_rules(rules: list[SenderRule], addrs: set[str]) -> list[SenderRule]:
    domains = {a.split("@", 1)[1] for a in addrs if "@" in a}
    out: list[SenderRule] = []
    for rule in rules:
        if rule.match_type == "address" and rule.pattern in addrs:
            out.append(rule)
        elif rule.match_type == "domain" and rule.pattern in domains:
            out.append(rule)
    return out


def matching_label_ids(rules: list[SenderRule], addrs: set[str]) -> set[uuid.UUID]:
    return {
        r.label_id for r in matching_rules(rules, addrs)
        if (r.action or "label") == "label" and r.label_id is not None
    }


def delegate_rule(rules: list[SenderRule], addr: str) -> SenderRule | None:
    """The delegate rule for a sender — an address rule beats a domain rule."""
    hits = [r for r in matching_rules(rules, {addr}) if r.action == "delegate" and r.value]
    hits.sort(key=lambda r: 0 if r.match_type == "address" else 1)
    return hits[0] if hits else None


async def ignored_by_rule(session: AsyncSession, email: Email) -> bool:
    """True when an inbound message comes from a sender the user asked to ignore. Only
    inbound: an ignore rule on a domain must not silence my own mail to that domain."""
    if email.direction != Direction.INBOUND:
        return False
    rules = (await session.execute(select(SenderRule))).scalars().all()
    addr = normalize_addr(email.from_addr or "")
    return any(r.action == "ignore" for r in matching_rules(list(rules), {addr}))


async def _attach(session: AsyncSession, email_id: uuid.UUID, label_ids: set[uuid.UUID]) -> None:
    if not label_ids:
        return
    existing = {
        lid
        for (lid,) in await session.execute(
            select(EmailLabel.label_id).where(EmailLabel.email_id == email_id)
        )
    }
    for lid in label_ids - existing:
        session.add(EmailLabel(email_id=email_id, label_id=lid))


async def apply_rules_to_email(session: AsyncSession, email: Email) -> None:
    rules = (await session.execute(select(SenderRule))).scalars().all()
    if not rules:
        return
    ids = matching_label_ids(list(rules), correspondents(email))
    await _attach(session, email.id, ids)


async def apply_rules_to_existing(session: AsyncSession) -> int:
    rules = list((await session.execute(select(SenderRule))).scalars().all())
    if not rules:
        return 0
    emails = (await session.execute(select(Email))).scalars().all()
    touched = 0
    for email in emails:
        ids = matching_label_ids(rules, correspondents(email))
        if ids:
            await _attach(session, email.id, ids)
            touched += 1
    return touched


# ---- Display ----
async def labels_for_emails(
    session: AsyncSession, email_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[Label]]:
    if not email_ids:
        return {}
    rows = await session.execute(
        select(EmailLabel.email_id, Label)
        .join(Label, EmailLabel.label_id == Label.id)
        .where(EmailLabel.email_id.in_(email_ids))
    )
    out: dict[uuid.UUID, list[Label]] = {}
    for eid, lbl in rows:
        out.setdefault(eid, []).append(lbl)
    return out


async def labels_for_items(session: AsyncSession, items: list) -> dict:
    """Метки карточки — метки писем её беседы (docs/ux-tasks.md §17.6).

    Правило вешает метку на письма конкретного адреса, а карточка вырастает из беседы с
    этим же человеком: метка на строке задачи отвечает на «от кого это», тем же чипом,
    что и в почте. Карточки без беседы (встреча, Telegram, ручная) меток не имеют."""
    thread_ids = {i.source_thread_id for i in items if i.source_thread_id}
    if not thread_ids:
        return {}
    rows = await session.execute(
        select(Email.thread_id, Label)
        .join(EmailLabel, EmailLabel.email_id == Email.id)
        .join(Label, EmailLabel.label_id == Label.id)
        .where(Email.thread_id.in_(thread_ids))
    )
    by_thread: dict[uuid.UUID, list[Label]] = {}
    for tid, lbl in rows:
        seen = by_thread.setdefault(tid, [])
        if all(x.id != lbl.id for x in seen):
            seen.append(lbl)
    for seen in by_thread.values():
        seen.sort(key=lambda x: x.name)
    return {
        i.id: by_thread[i.source_thread_id]
        for i in items
        if i.source_thread_id in by_thread
    }


async def email_ids_for_label(session: AsyncSession, label_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.execute(
        select(EmailLabel.email_id).where(EmailLabel.label_id == label_id)
    )
    return [eid for (eid,) in rows]
