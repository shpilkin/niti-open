"""«Запомнить» from a card: one line the user wants the assistant to keep.

Two places to keep it, both already read by the prompts (memory/facts.py):
- about the counterparty → a fact on the person (global: who this person is does not
  depend on the space);
- about myself / my teams / how things are done here → a fact in the space profile (per
  space, the «core memory» every extraction in the space sees).

Each fact carries its date so it can later be judged by its age, and the act itself is
journaled in agent_decisions like every other user action on a card.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from email_reader import profiles
from email_reader.identities import get_identity_set
from email_reader.memory import people as people_svc
from email_reader.memory.context import external_parties
from email_reader.models import ActionItem, AgentDecision, Email

TARGET_PERSON = "person"
TARGET_PROFILE = "profile"


async def counterparty_address(
    session: AsyncSession, item: ActionItem
) -> tuple[str | None, str | None]:
    """(address, name) of the card's counterparty as read off its thread.

    A card without a thread (meeting, Telegram, typed by hand) has no address, but it does
    have a counterparty the model named — `counterparty_target` turns that into a person
    to remember, so «запомнить о человеке» is no longer mail-only."""
    if item.source_thread_id is None:
        return None, None
    emails = list(
        (
            await session.execute(
                select(Email)
                .options(load_only(
                    Email.id, Email.direction, Email.from_addr, Email.from_name,
                    Email.to_addrs, Email.cc_addrs, Email.sent_at,
                ))
                .where(Email.thread_id == item.source_thread_id)
                .order_by(Email.sent_at)
            )
        ).scalars().all()
    )
    parties = external_parties(emails, await get_identity_set(session))
    return parties[0] if parties else (None, None)


async def counterparty_target(
    session: AsyncSession, item: ActionItem
) -> tuple[str | None, str | None]:
    """(address-or-empty, display name) the «Запомнить» block offers for this card: the
    thread's party when there is a thread, else the counterparty the model named."""
    addr, name = await counterparty_address(session, item)
    if addr:
        return addr, name
    who = people_svc.display_of(item.counterparty or "")
    if not who or people_svc.identity_of(item.counterparty or "") is None:
        return None, None
    return "", who


async def remember(
    session: AsyncSession, *, item: ActionItem, target: str, text: str,
    addr: str | None, name: str | None, now: datetime | None = None,
) -> str:
    """Keep `text` where `target` says; returns the confirmation shown to the user."""
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("empty note")
    now = now or datetime.now(UTC)
    person = None
    if target == TARGET_PERSON:
        person = (
            await people_svc.ensure_person(session, addr, name, source=item.source_kind)
            if addr
            else await people_svc.ensure_for(
                session, item.counterparty or (name or ""), source=item.source_kind
            )
        )
    if person is not None:
        await people_svc.append_note(session, person, text, now=now)
        where = f"о {people_svc.display_name(person)}"
        payload = {"kind": TARGET_PERSON, "person_id": str(person.id), "addr": addr or None}
    else:
        await profiles.remember_line(session, item.space_id, text, now=now)
        where = "в профиль пространства"
        payload = {"kind": TARGET_PROFILE, "space_id": str(item.space_id)}
    session.add(
        AgentDecision(
            kind="user_action", action_item_id=item.id,
            payload={"action": "remember", "text": text, **payload},
        )
    )
    return f"Записано {where}."
