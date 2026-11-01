"""Learned rules: what the user keeps doing with a sender's cards, offered back as a rule.

Nothing here fires on its own. A pattern (three cards from this address marked «не
обязательство»; two cards handed to the same team) becomes a *proposal* in the person pane;
only after the user confirms it does a `SenderRule` exist — and then it runs
deterministically, before the model (`ignore`) or after the type gate (`delegate`).
See docs/feature-memory.md §3 stage 4 and §5.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import labels as labels_svc
from email_reader.enums import ActionStatus, ActionType, Direction, UserVerdict
from email_reader.models import ActionItem, Email, SenderRule
from email_reader.normalization.identity import normalize_addr
from email_reader.spaces import account_ids_of

LOOKBACK = 8        # recent cards from the sender that a pattern is judged on
MIN_DISMISSED = 3   # «не обязательство» this many times -> propose ignore
MIN_DELEGATED = 2   # handed to the same team this many times -> propose delegate


@dataclass
class Proposal:
    action: str  # ignore | delegate
    value: str | None
    match_type: str
    pattern: str
    evidence: str

    @property
    def text(self) -> str:
        return describe(self.action, self.value)


def describe(action: str, value: str | None) -> str:
    if action == "ignore":
        return "не разбирать письма"
    if action == "delegate":
        return f"делегировать: {value}"
    return "метка"


async def recent_items(
    session: AsyncSession, space_id: uuid.UUID, addr: str, limit: int = LOOKBACK
) -> list[ActionItem]:
    """The space's newest cards whose thread has a message from this address."""
    threads = (
        select(Email.thread_id)
        .where(
            Email.direction == Direction.INBOUND,
            func.lower(Email.from_addr) == addr,
            Email.thread_id.isnot(None),
            Email.account_id.in_(account_ids_of(space_id)),
        )
    )
    rows = await session.execute(
        select(ActionItem)
        .where(ActionItem.space_id == space_id, ActionItem.source_thread_id.in_(threads))
        .order_by(ActionItem.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())


async def propose_for(
    session: AsyncSession, space_id: uuid.UUID | None, addr: str
) -> list[Proposal]:
    addr = normalize_addr(addr)
    if not addr or space_id is None:
        return []
    existing = {(r.action, r.value) for r in await labels_svc.rules_for_address(session, addr)}
    items = await recent_items(session, space_id, addr)
    if not items:
        return []
    out: list[Proposal] = []

    dismissed = [
        i for i in items
        if i.status == ActionStatus.DISMISSED and i.resolved_by == "user"
    ]
    kept = [
        i for i in items
        if i.user_verdict in (UserVerdict.CONFIRMED, UserVerdict.EDITED)
        or i.status == ActionStatus.DONE
    ]
    if len(dismissed) >= MIN_DISMISSED and not kept and ("ignore", None) not in existing:
        out.append(Proposal(
            "ignore", None, "address", addr,
            f"{len(dismissed)} из последних {len(items)} карточек от этого адреса ты "
            f"пометил(а) «не обязательство»",
        ))

    handed = Counter(
        (i.delegate_to or "").strip()
        for i in items
        if i.type == ActionType.TO_DELEGATE and (i.delegate_to or "").strip()
        and i.status != ActionStatus.DISMISSED
    )
    if handed:
        who, n = handed.most_common(1)[0]
        if n >= MIN_DELEGATED and ("delegate", who) not in existing:
            out.append(Proposal(
                "delegate", who, "address", addr,
                f"{n} из последних {len(items)} карточек от этого адреса — делегировать {who}",
            ))
    return out


async def accept(
    session: AsyncSession, *, match_type: str, pattern: str, action: str, value: str | None,
) -> SenderRule | None:
    """A confirmed proposal (or a rule typed by hand) becomes a SenderRule."""
    if action not in ("ignore", "delegate"):
        return None
    return await labels_svc.create_rule(
        session, match_type, pattern, None, action=action, value=value
    )
