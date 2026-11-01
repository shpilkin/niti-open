"""Подсказка получателей при вводе адреса.

Источник — переписка, а не список профилей: профилей в базе единицы, а людей, с
которыми человек уже переписывался, сотни. Так же ведут себя почтовые клиенты
(«previous recipients»). Профили людей добавляются сверху: у них есть имя, заданное
руками, и по нему тоже надо находить.

Предлагаются только адреса — контакт без почты в поле «Кому» вставить нечего.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.enums import Direction, IdentityKind
from email_reader.identities import get_identity_set
from email_reader.models import Account, Email, Person, PersonIdentity

MIN_QUERY = 2
_SCAN_LIMIT = 2000  # свежих корреспондентов на просмотр — не вся база


def _fold(value: str | None) -> str:
    """Ключ сравнения: «Пётр» находится по «петр» — иначе половина имён не ищется."""
    return (value or "").strip().lower().replace("ё", "е")


@dataclass(frozen=True)
class Suggestion:
    email: str
    name: str | None

    @property
    def label(self) -> str:
        return f"{self.name} <{self.email}>" if self.name else self.email


async def suggest(
    session: AsyncSession, q: str, *, space_id: uuid.UUID | None = None, limit: int = 8
) -> list[Suggestion]:
    """Адреса, подходящие под введённое: по адресу или по имени, без учёта регистра."""
    q = _fold(q)
    if len(q) < MIN_QUERY:
        return []
    mine = await get_identity_set(session)
    found: dict[str, Suggestion] = {}

    def offer(email: str | None, name: str | None) -> None:
        addr = (email or "").strip().lower()
        if not addr or "@" not in addr or addr in mine or addr in found:
            return
        if q not in _fold(addr) and q not in _fold(name):
            return
        found[addr] = Suggestion(email=addr, name=(name or "").strip() or None)

    # Отбор — в Python, а не в SQL: lower() в SQLite не трогает не-ASCII, и «Голубев»
    # по запросу «голубев» не находился, хотя «golub» находился.
    rows = select(
        Email.from_addr, func.max(Email.from_name), func.max(Email.sent_at).label("last")
    ).group_by(Email.from_addr)
    if space_id is not None:
        # Ящики смешиваются внутри пространства и никогда между ними (docs/spaces.md).
        rows = rows.where(
            Email.account_id.in_(select(Account.id).where(Account.space_id == space_id))
        )
    for addr, name, _last in (
        await session.execute(rows.order_by(func.max(Email.sent_at).desc()).limit(_SCAN_LIMIT))
    ).all():
        offer(addr, name)

    # Кому писал я сам: такой человек может не написать в ответ ни разу, и из входящих
    # он не виден вовсе. Отправленных писем на порядок меньше — читаем их целиком.
    sent = select(Email.to_addrs, Email.cc_addrs).where(
        Email.direction.in_([Direction.OUTBOUND, Direction.SELF])
    )
    if space_id is not None:
        sent = sent.where(
            Email.account_id.in_(select(Account.id).where(Account.space_id == space_id))
        )
    for to_addrs, cc_addrs in (
        await session.execute(sent.order_by(Email.sent_at.desc()).limit(_SCAN_LIMIT))
    ).all():
        for row in list(to_addrs or []) + list(cc_addrs or []):
            offer(row.get("email"), row.get("name"))

    # Профиль человека: имя в нём задано руками, и по нему тоже ищут.
    for person, ident in (await session.execute(
        select(Person, PersonIdentity)
        .join(PersonIdentity, PersonIdentity.person_id == Person.id)
        .where(Person.is_me.is_(False), PersonIdentity.kind == IdentityKind.EMAIL.value)
    )).all():
        offer(ident.value, person.name or ident.display)

    # Порядок вставки — по свежести переписки: с кем писались недавно, тот и нужен чаще.
    return list(found.values())[:limit]
