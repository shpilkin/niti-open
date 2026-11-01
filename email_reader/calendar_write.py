"""Создание и правка событий из Niti (docs/feature-calendar.md §11).

Календари ящиков с CalDAV (например, Яндекс): событие пишется прямо на сервер,
в Niti кладётся сразу, не дожидаясь следующей сверки.

Приглашения — выбор владельца при каждом сохранении, по умолчанию «только себе»:
- без рассылки участники не попадают в ATTENDEE (сервер разослал бы им письма), а
  записываются строкой «Участники: …» в описании — так их видно в любом календаре и можно
  пригласить позже, исправив событие;
- с рассылкой — ORGANIZER и ATTENDEE, приглашения рассылает сам сервер (RFC 6638);
- правка уже разосланного без оповещения — ATTENDEE с SCHEDULE-AGENT=CLIENT: сервер
  изменений участникам не шлёт.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import spaces as spaces_svc
from email_reader.connectors import caldav as caldav_conn
from email_reader.models import Account, CalendarEvent
from email_reader.search import index as search_index

log = logging.getLogger("email_reader.calendar_write")

PARTICIPANTS_PREFIX = "Участники:"
DEFAULT_CALENDAR = "Мои события"


class WriteError(Exception):
    """Сервер календаря не принял событие; текст — для владельца."""


@dataclass(frozen=True)
class Target:
    """Куда можно записать событие: календарь ящика."""

    key: str
    account: Account
    calendar: str
    label: str


@dataclass
class Draft:
    title: str
    start: datetime            # aware; у события на весь день — полночь UTC первого дня
    end: datetime              # aware; у события на весь день — полночь UTC дня после
    all_day: bool = False
    location: str = ""
    description: str = ""
    participants: list[str] = field(default_factory=list)   # адреса
    invite: bool = False       # разослать приглашения / изменения участникам


async def targets(session: AsyncSession, space_id: uuid.UUID) -> list[Target]:
    """Календари, в которые можно писать: у ящика этого пространства есть CalDAV, а
    календарь уже присылал события (список задач «Не забыть» их не присылает)."""
    accounts = (await session.execute(
        select(Account).where(Account.id.in_(spaces_svc.account_ids_of(space_id)),
                              Account.enabled.is_(True))
        .order_by(Account.created_at)
    )).scalars().all()
    out: list[Target] = []
    for acc in accounts:
        if not caldav_conn.caldav_base_url(acc.provider):
            continue
        names = (await session.execute(
            select(CalendarEvent.calendar_name).where(
                CalendarEvent.account_id == acc.id,
                CalendarEvent.calendar_name.is_not(None),
            ).distinct()
        )).scalars().all()
        for name in sorted(names, key=lambda n: (n != DEFAULT_CALENDAR, n)):
            out.append(Target(f"caldav:{acc.id}:{name}", acc, name, f"{acc.name} · {name}"))
    return out


def target_of(options: list[Target], key: str) -> Target | None:
    return next((t for t in options if t.key == key), None)


def participants_of(event: CalendarEvent) -> list[str]:
    """Участники события: приглашённые, а если не приглашали — строка из описания."""
    own = {(event.organizer or "").lower()}
    got = [a.get("email") for a in event.attendees or [] if a.get("email")]
    if not got:
        for line in (event.description or "").splitlines():
            if line.startswith(PARTICIPANTS_PREFIX):
                got = [p.strip() for p in line[len(PARTICIPANTS_PREFIX):].split(",")]
    return [p for p in got if p and p.lower() not in own]


def _clean_description(text: str) -> str:
    return "\n".join(line for line in (text or "").splitlines()
                     if not line.startswith(PARTICIPANTS_PREFIX)).strip()


def build_ical(uid: str, draft: Draft, organizer: str, *, sequence: int = 0,
               silent_attendees: bool = False) -> str:
    """VCALENDAR одного события. `silent_attendees` — участники остаются в событии, но
    сервер не шлёт им изменений (правка без оповещения)."""
    from icalendar import Calendar, Event, vCalAddress, vText

    cal = Calendar()
    cal.add("prodid", "-//Niti//RU")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", uid)
    ev.add("dtstamp", datetime.now(UTC))
    ev.add("sequence", sequence)
    ev.add("summary", draft.title.strip() or "Без названия")
    if draft.all_day:
        ev.add("dtstart", draft.start.date())
        ev.add("dtend", draft.end.date())
    else:
        ev.add("dtstart", draft.start.astimezone(UTC))
        ev.add("dtend", draft.end.astimezone(UTC))
    if draft.location.strip():
        ev.add("location", draft.location.strip())
    description = _clean_description(draft.description)
    people = [p for p in draft.participants if p]
    if people and (draft.invite or silent_attendees):
        org = vCalAddress(f"mailto:{organizer}")
        org.params["cn"] = vText(organizer)
        ev.add("organizer", org)
        for email in people:
            who = vCalAddress(f"mailto:{email}")
            who.params["role"] = vText("REQ-PARTICIPANT")
            who.params["partstat"] = vText("NEEDS-ACTION")
            who.params["rsvp"] = vText("TRUE")
            if not draft.invite:
                who.params["schedule-agent"] = vText("CLIENT")
            ev.add("attendee", who, encode=0)
    elif people:
        description = (f"{PARTICIPANTS_PREFIX} {', '.join(people)}\n\n{description}").strip()
    if description:
        ev.add("description", description)
    cal.add_component(ev)
    return cal.to_ical().decode()


def _calendar_by_name(principal, name: str):  # noqa: ANN001, ANN202
    for cal in principal.calendars():
        if cal.name == name:
            return cal
    raise WriteError(f"Календарь «{name}» не найден на сервере.")


def _put_blocking(base: str, login: str, password: str, calendar: str, uid: str,
                  ical: str, *, create: bool) -> None:
    import caldav

    with caldav.DAVClient(url=base, username=login, password=password,
                          timeout=caldav_conn._TIMEOUT) as client:
        cal = _calendar_by_name(client.principal(), calendar)
        if create:
            cal.add_event(ical)
            return
        obj = cal.get_event_by_uid(uid)
        obj.data = _next_sequence(ical, obj.icalendar_component.get("sequence", 0))
        obj.save(increase_seqno=False)


def _delete_blocking(base: str, login: str, password: str, calendar: str, uid: str,
                     quiet_ical: str | None) -> None:
    """Удалить событие. `quiet_ical` — сначала пометить участников SCHEDULE-AGENT=CLIENT,
    чтобы сервер не разослал им отмену (удаление без оповещения)."""
    import caldav

    with caldav.DAVClient(url=base, username=login, password=password,
                          timeout=caldav_conn._TIMEOUT) as client:
        cal = _calendar_by_name(client.principal(), calendar)
        obj = cal.get_event_by_uid(uid)
        if quiet_ical:
            obj.data = _next_sequence(quiet_ical, obj.icalendar_component.get("sequence", 0))
            obj.save(increase_seqno=False)
        obj.delete()


def _next_sequence(ical: str, current) -> str:  # noqa: ANN001
    """Номер версии — на единицу больше серверного: по нему участники отличают
    изменение от старой копии (RFC 5545 §3.8.7.4)."""
    from icalendar import Calendar

    cal = Calendar.from_ical(ical)
    for comp in cal.walk("VEVENT"):
        comp.pop("sequence", None)
        comp.add("sequence", int(current or 0) + 1)
    return cal.to_ical().decode()


async def _put(account: Account, calendar: str, uid: str, ical: str, *, create: bool) -> None:
    from email_reader.accounts import resolve_caldav_password

    base = caldav_conn.caldav_base_url(account.provider)
    if not base:
        raise WriteError("У этого ящика нет календаря, в который Niti умеет писать.")
    try:
        await asyncio.to_thread(_put_blocking, base, account.login,
                                resolve_caldav_password(account), calendar, uid, ical,
                                create=create)
    except WriteError:
        raise
    except Exception as exc:  # noqa: BLE001 — владельцу нужна причина, а не трасса
        log.warning("Calendar write failed for %s: %s", account.name, exc)
        raise WriteError(f"Сервер календаря не принял событие: {str(exc)[:200]}") from exc


def _store(row: CalendarEvent, draft: Draft, organizer: str) -> None:
    row.title = draft.title.strip() or "Без названия"
    row.start_at, row.end_at = draft.start, draft.end
    row.location = draft.location.strip() or None
    people = [p for p in draft.participants if p]
    description = _clean_description(draft.description)
    if people and not row.attendees and not draft.invite:
        description = (f"{PARTICIPANTS_PREFIX} {', '.join(people)}\n\n{description}").strip()
    row.description = description or None
    if draft.invite and people:
        row.organizer = organizer
        row.attendees = [{"email": p, "name": None} for p in people]
    row.cancelled_at = None


async def create(session: AsyncSession, target: Target, draft: Draft) -> CalendarEvent:
    """Записать новое событие на сервер и сразу положить в Niti."""
    uid = f"{uuid.uuid4()}@niti"
    organizer = target.account.login
    await _put(target.account, target.calendar, uid, build_ical(uid, draft, organizer),
               create=True)
    row = CalendarEvent(account_id=target.account.id, provider_uid=uid,
                        calendar_name=target.calendar, start_at=draft.start,
                        end_at=draft.end, attendees=[])
    _store(row, draft, organizer)
    session.add(row)
    await session.flush()
    await search_index.index_event(session, row)
    return row


def editable(
    event: CalendarEvent, account: Account | None,
) -> str | None:
    """None — можно исправить; иначе — почему нельзя, словами для владельца."""
    if account is None or not caldav_conn.caldav_base_url(account.provider):
        return "Это событие Niti пока исправлять не умеет — только в календаре, откуда оно."
    return _obstacle(event, account)


def _obstacle(event: CalendarEvent, account: Account) -> str | None:
    if "#" in event.provider_uid:
        return "Повторяющуюся встречу исправьте в Календаре: одно повторение Niti не правит."
    org = (event.organizer or "").lower()
    if org and org != (account.login or "").lower():
        return "Встречу назначил другой человек — исправить её может только он."
    if not event.calendar_name:
        return "Неизвестно, в каком календаре лежит событие, — дождитесь следующей сверки."
    return None


async def update(session: AsyncSession, event: CalendarEvent, draft: Draft) -> CalendarEvent:
    """Исправить событие на сервере. Уже приглашённые остаются в событии; оповещать ли
    их об изменении — `draft.invite`."""
    account = await session.get(Account, event.account_id)
    why = editable(event, account)
    if why:
        raise WriteError(why)
    already = bool(event.attendees)
    ical = build_ical(event.provider_uid, draft, account.login,
                      silent_attendees=already and not draft.invite)
    await _put(account, event.calendar_name, event.provider_uid, ical, create=False)
    _store(event, draft, account.login)
    await session.flush()
    await search_index.index_event(session, event)
    return event


def parse_draft(*, title: str, day: str, start: str, end: str, all_day: bool,
                location: str, description: str, participants: str, invite: bool,
                tz) -> Draft:  # noqa: ANN001
    """Поля формы → Draft. Время — в поясе Mac; у события на весь день — даты."""
    first = date.fromisoformat(day)
    people = [p.strip() for p in participants.replace(";", ",").split(",") if p.strip()]
    if all_day:
        s = datetime(first.year, first.month, first.day, tzinfo=UTC)
        return Draft(title, s, s + timedelta(days=1), True, location, description, people,
                     invite)
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    s = datetime(first.year, first.month, first.day, sh, sm, tzinfo=tz)
    e = datetime(first.year, first.month, first.day, eh, em, tzinfo=tz)
    if e <= s:
        e = s + timedelta(hours=1)
    return Draft(title, s.astimezone(UTC), e.astimezone(UTC), False, location, description,
                 people, invite)


def _draft_of(event: CalendarEvent) -> Draft:
    from email_reader import calendar_grid

    return Draft(title=event.title or "", start=event.start_at,
                 end=event.end_at or event.start_at, location=event.location or "",
                 description=event.description or "",
                 participants=participants_of(event),
                 all_day=calendar_grid.is_all_day(event.start_at, event.end_at))


async def delete(session: AsyncSession, event: CalendarEvent, notify: bool) -> CalendarEvent:
    """Удалить событие. В Niti оно остаётся зачёркнутым — как пропавшее с сервера: на него
    могут ссылаться карточки. `notify` — разослать участникам отмену."""
    account = await session.get(Account, event.account_id)
    now = datetime.now(UTC)
    why = editable(event, account)
    if why:
        raise WriteError(f"Удалить нельзя: {why[0].lower()}{why[1:]}")
    quiet = None
    if event.attendees and not notify:
        quiet = build_ical(event.provider_uid, _draft_of(event), account.login,
                           silent_attendees=True)
    base = caldav_conn.caldav_base_url(account.provider)
    from email_reader.accounts import resolve_caldav_password

    try:
        await asyncio.to_thread(_delete_blocking, base, account.login,
                                resolve_caldav_password(account), event.calendar_name,
                                event.provider_uid, quiet)
    except Exception as exc:  # noqa: BLE001 — владельцу нужна причина, а не трасса
        log.warning("Calendar delete failed for %s: %s", account.name, exc)
        raise WriteError(f"Сервер календаря не удалил событие: {str(exc)[:200]}") from exc
    event.cancelled_at = now
    await session.flush()
    return event
