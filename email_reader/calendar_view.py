"""Календарь для экрана: события недели пространства и то, что Niti знает о событии.

Раскладка по сетке — в `calendar_grid` (чистые функции); здесь только чтение базы.
Пространство — через ящик события: календарь принадлежит ящику, ящик — пространству
(docs/spaces.md). Чужое пространство в выдачу не попадает ни строкой.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import calendar_grid
from email_reader import spaces as spaces_svc
from email_reader.models import Account, CalendarEvent, Meeting

# Запись без длительности считаем часовой — чтобы было с чем пересекать событие.
_RECORDING_FALLBACK = timedelta(hours=1)


# Цвета календарей — токены --cal-1 … --cal-6 (static/tokens.css), по кругу.
PALETTE = 6


def source_key(account_id: uuid.UUID | None, calendar_name: str | None) -> str:
    """Короткий ключ календаря для адреса (`hide=`): в имени календаря бывают запятые."""
    raw = f"{account_id}:{calendar_name or ''}".encode()
    return hashlib.sha1(raw).hexdigest()[:8]  # noqa: S324 — не защита, а короткое имя


def key_of(event: CalendarEvent) -> str:
    return source_key(event.account_id, event.calendar_name)


@dataclass(frozen=True)
class Source:
    """Календарь на экране: ящик и календарь внутри него, со своим цветом."""

    key: str
    account: Account
    calendar_name: str | None
    label: str
    color: int


async def _between(
    session: AsyncSession, space_id: uuid.UUID, first: date, days: int,
    tz: tzinfo | None, hidden: frozenset[str],
) -> list[CalendarEvent]:
    lo, hi = calendar_grid.bounds_utc(first, tz, days)
    q = select(CalendarEvent).where(
        CalendarEvent.account_id.in_(spaces_svc.account_ids_of(space_id)),
        CalendarEvent.start_at < hi,
        func.coalesce(CalendarEvent.end_at, CalendarEvent.start_at) >= lo,
    )
    rows = (await session.execute(q.order_by(CalendarEvent.start_at))).scalars().all()
    return [ev for ev in rows if key_of(ev) not in hidden]


async def week(
    session: AsyncSession, space_id: uuid.UUID, week_start: date, *, days: int = 7,
    tz: tzinfo | None = None, now: datetime | None = None,
    hidden: frozenset[str] = frozenset(),
) -> calendar_grid.Week:
    """Неделя (или день, `days=1`) пространства сеткой. Отменённые события остаются —
    сетка их зачёркивает. `hidden` — ключи календарей, которые владелец выключил."""
    rows = await _between(session, space_id, week_start, days, tz, hidden)
    return calendar_grid.layout(rows, week_start, days_count=days, tz=tz, now=now)


async def month(
    session: AsyncSession, space_id: uuid.UUID, first: date, *,
    tz: tzinfo | None = None, now: datetime | None = None,
    hidden: frozenset[str] = frozenset(),
) -> calendar_grid.Month:
    start, end = calendar_grid.month_bounds(first.replace(day=1))
    rows = await _between(session, space_id, start, (end - start).days, tz, hidden)
    return calendar_grid.month_layout(rows, first, tz=tz, now=now)


async def calendars(session: AsyncSession, space_id: uuid.UUID) -> list[Source]:
    """Календари пространства, в которых есть события, — каждый своим цветом.

    Цвет — по порядку, в котором календарь впервые появился в Niti: новый календарь
    берёт следующий цвет и не перекрашивает соседей. Событие, пришедшее до того, как
    Niti начала запоминать календарь, стоит в календаре без имени — под именем ящика.
    """
    first_seen = func.min(CalendarEvent.created_at)
    pairs = (await session.execute(
        select(CalendarEvent.account_id, CalendarEvent.calendar_name)
        .where(CalendarEvent.account_id.in_(spaces_svc.account_ids_of(space_id)))
        .group_by(CalendarEvent.account_id, CalendarEvent.calendar_name)
        .order_by(first_seen, CalendarEvent.calendar_name)
    )).all()
    accounts = {a.id: a for a in (await session.execute(
        select(Account).where(Account.id.in_({p.account_id for p in pairs}))
    )).scalars()}
    names = [p.calendar_name for p in pairs if p.calendar_name]
    named_accounts = {p.account_id for p in pairs if p.calendar_name}
    out = []
    for i, (account_id, name) in enumerate(pairs):
        account = accounts[account_id]
        # Одинаково названные календари разных ящиков («Календарь») — с именем ящика;
        # безымянный рядом с названными — «прочие»: удалённые и давние встречи.
        if name:
            label = name if names.count(name) == 1 else f"{account.name} · {name}"
        else:
            label = f"{account.name} · прочие" if account_id in named_accounts else account.name
        out.append(Source(source_key(account_id, name), account, name, label,
                          i % PALETTE + 1))
    return out


async def get_event(
    session: AsyncSession, event_id: uuid.UUID, space_id: uuid.UUID
) -> CalendarEvent | None:
    """Событие — только если его ящик в этом пространстве."""
    return (await session.execute(
        select(CalendarEvent).where(
            CalendarEvent.id == event_id,
            CalendarEvent.account_id.in_(spaces_svc.account_ids_of(space_id)),
        )
    )).scalars().first()


async def mailbox_of(session: AsyncSession, event: CalendarEvent) -> Account | None:
    return await session.get(Account, event.account_id) if event.account_id else None


async def recordings_for(
    session: AsyncSession, event: CalendarEvent, space_id: uuid.UUID
) -> list[Meeting]:
    """Записи встреч того же пространства, которые шли в то же время, что событие.

    Склейка по пересечению времени: запись начинают не секунда в секунду с приглашением,
    а за пару минут до или с опозданием. Участников не сверяем — у записи их часто нет.
    """
    ev_end = event.end_at if event.end_at and event.end_at > event.start_at else (
        event.start_at + timedelta(minutes=calendar_grid.NO_END_MINUTES))
    rows = (await session.execute(
        select(Meeting).where(
            Meeting.space_id == space_id,
            Meeting.deleted_at.is_(None),
            Meeting.started_at < ev_end,
            Meeting.started_at >= event.start_at - timedelta(hours=12),
        ).order_by(Meeting.started_at)
    )).scalars().all()
    out = []
    for m in rows:
        length = (timedelta(seconds=m.duration_sec) if m.duration_sec
                  else _RECORDING_FALLBACK)
        if m.started_at + length > event.start_at:
            out.append(m)
    return out
