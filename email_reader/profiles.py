"""Per-space owner profile: who I am in this space, my role, and my teams.

Since facts (docs/feature-memory-facts.md) the profile is not a text in `app_settings`
but the `me` facts of the space, rendered back into one block: the editor's text first,
then every «Запомнить» line as `- date: text` — the same text the prompts got before.
Two readers, two renderings. `core` is the editor's text alone — what the thread prompts
(extractor, drafts, eval) get as the always-on part; the remembered lines reach them by
relevance through `memory/context`. `get_profile` is everything, for the readers that have
no thread to be relevant to: the briefing, MCP `get_context`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.memory import facts
from email_reader.models import MemoryFact

# Shown as the editor placeholder to guide what to write (role + teams + who does what).
PLACEHOLDER = (
    "Роль: руководитель направления, отвечаю за логистику.\n"
    "Команды:\n"
    "- Продажи (Оля, Роман) — клиенты, договоры, счета.\n"
    "- Склад (Борис) — приёмка, отгрузки, инвентаризация.\n"
    "- Бухгалтерия — оплаты и отчётность.\n"
    "Чем конкретнее зоны ответственности — тем точнее ассистент подскажет, кому делегировать."
)


async def _facts(session: AsyncSession, space_id: uuid.UUID) -> list[MemoryFact]:
    return await facts.active(session, subject_kind=facts.ME, space_id=space_id)


async def get_profile(session: AsyncSession, space_id: uuid.UUID | None) -> str:
    """The block the prompts get: editor text, then the remembered lines."""
    if space_id is None:
        return ""
    return facts.render(await _facts(session, space_id))


async def core(session: AsyncSession, space_id: uuid.UUID | None) -> str:
    """The profile fact alone: what the editor shows, what a thread prompt always gets."""
    if space_id is None:
        return ""
    return "\n".join(
        f.text for f in await _facts(session, space_id) if f.evidence_kind == facts.EV_PROFILE
    )


async def remembered(session: AsyncSession, space_id: uuid.UUID) -> list[MemoryFact]:
    """The lines under the editor: what was remembered from cards and MCP, and what
    capture found and the owner confirmed — everything of mine that is not the editor's
    own text."""
    return [f for f in await _facts(session, space_id) if f.evidence_kind != facts.EV_PROFILE]


async def set_profile(session: AsyncSession, space_id: uuid.UUID, value: str) -> None:
    """Replace the editor's fact; the remembered lines are not touched."""
    text = (value or "").strip()
    old = [f for f in await _facts(session, space_id) if f.evidence_kind == facts.EV_PROFILE]
    if len(old) == 1 and old[0].text == text:
        return
    for fact in old:
        await facts.retire(session, fact)
    if text:
        await facts.add(
            session, subject_kind=facts.ME, space_id=space_id, text=text,
            evidence_kind=facts.EV_PROFILE, supersedes=old[0] if old else None,
            seq=facts._PROFILE_SEQ,
        )


async def remember_line(
    session: AsyncSession, space_id: uuid.UUID, text: str, now: datetime | None = None
) -> MemoryFact:
    """«Запомнить» about myself / my teams / how things are done here."""
    return await facts.add(
        session, subject_kind=facts.ME, space_id=space_id, text=text,
        evidence_kind=facts.EV_REMEMBER, now=now,
    )


async def forget(session: AsyncSession, space_id: uuid.UUID, fact_id: uuid.UUID) -> bool:
    """Retire one remembered line of the space. False when there is no such line."""
    fact = await session.get(MemoryFact, fact_id)
    if (fact is None or fact.space_id != space_id or fact.subject_kind != facts.ME
            or fact.valid_to is not None):
        return False
    await facts.retire(session, fact)
    return True
