"""Создание и правка событий из Niti (docs/feature-calendar.md §11): что уходит на сервер
календаря и что остаётся в Niti. Сервер — заглушка: настоящий календарь владельца тесты
не трогают."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar
from sqlalchemy import select

from email_reader import calendar_view, calendar_write
from email_reader.models import Account, CalendarEvent
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space

MSK = ZoneInfo("Europe/Moscow")


def _event_of(ical: str):
    return Calendar.from_ical(ical).walk("VEVENT")[0]


def _draft(**kw) -> calendar_write.Draft:
    base = dict(title="Созвон по смете", start=datetime(2026, 10, 1, 7, tzinfo=UTC),
                end=datetime(2026, 10, 1, 8, tzinfo=UTC), participants=["anna@work.ru"])
    base.update(kw)
    return calendar_write.Draft(**base)


def test_only_for_me_by_default_nobody_is_invited_but_nobody_is_lost():
    ev = _event_of(calendar_write.build_ical("u1", _draft(), "me@corpmail.ru"))
    assert "ATTENDEE" not in ev and "ORGANIZER" not in ev
    assert str(ev["DESCRIPTION"]).startswith("Участники: anna@work.ru")


def test_inviting_names_me_the_organizer_and_asks_the_server_to_send():
    ev = _event_of(calendar_write.build_ical("u1", _draft(invite=True), "me@corpmail.ru"))
    assert str(ev["ORGANIZER"]) == "mailto:me@corpmail.ru"
    who = ev["ATTENDEE"]
    assert str(who) == "mailto:anna@work.ru" and who.params["RSVP"] == "TRUE"
    assert "SCHEDULE-AGENT" not in who.params
    assert "DESCRIPTION" not in ev


def test_editing_an_invited_meeting_quietly_keeps_the_guests_but_sends_nothing():
    ev = _event_of(calendar_write.build_ical("u1", _draft(), "me@corpmail.ru",
                                             silent_attendees=True))
    assert ev["ATTENDEE"].params["SCHEDULE-AGENT"] == "CLIENT"


def test_an_all_day_event_is_dates_not_midnights():
    d = calendar_write.parse_draft(title="Отпуск", day="2026-10-05", start="", end="",
                                   all_day=True, location="", description="",
                                   participants="", invite=False, tz=MSK)
    ev = _event_of(calendar_write.build_ical("u1", d, "me@corpmail.ru"))
    assert ev["DTSTART"].dt == date(2026, 10, 5) and ev["DTEND"].dt == date(2026, 10, 6)


def test_the_form_time_is_the_macs_local_time():
    d = calendar_write.parse_draft(title="x", day="2026-10-01", start="10:00", end="09:00",
                                   all_day=False, location="", description="",
                                   participants="a@x.ru; b@y.ru", invite=False, tz=MSK)
    assert d.start == datetime(2026, 10, 1, 7, tzinfo=UTC)
    assert d.end == datetime(2026, 10, 1, 8, tzinfo=UTC)   # конец раньше начала → час
    assert d.participants == ["a@x.ru", "b@y.ru"]


def test_the_next_version_number_follows_the_servers():
    raw = calendar_write.build_ical("u1", _draft(), "me@corpmail.ru")
    assert _event_of(calendar_write._next_sequence(raw, 4))["SEQUENCE"] == 5


async def _sbs(s):
    await mk_space(s, 1, name="Рабочее")
    await mk_space(s, 2, name="Личная")
    work = Account(space_id=SPACE_ID, name="work", provider="yandex", host="imap.yandex.ru",
                  login="me@corpmail.ru", folders=[])
    home = Account(space_id=SPACE_ID_2, name="home", provider="yandex", host="imap.yandex.ru",
                   login="me@ya.ru", folders=[])
    s.add_all([work, home])
    await s.flush()
    rows = [(work, "Мои события", "a"), (work, "резерв", "b"), (home, "Дом", "c")]
    for acc, name, uid in rows:
        s.add(CalendarEvent(account_id=acc.id, provider_uid=uid, calendar_name=name,
                            start_at=datetime(2026, 9, 1, 7, tzinfo=UTC),
                            end_at=datetime(2026, 9, 1, 8, tzinfo=UTC), attendees=[]))
    await s.flush()
    return work


async def test_only_this_spaces_server_calendars_are_offered(db):
    async with db() as s:
        await _sbs(s)
        got = await calendar_write.targets(s, SPACE_ID)
    assert [t.label for t in got] == ["work · Мои события", "work · резерв"]


async def test_a_created_event_is_in_niti_at_once(db, monkeypatch):
    sent = {}

    async def fake_put(account, calendar, uid, ical, *, create):
        sent.update(account=account.name, calendar=calendar, uid=uid, create=create)

    monkeypatch.setattr(calendar_write, "_put", fake_put)
    async with db() as s:
        await _sbs(s)
        target = (await calendar_write.targets(s, SPACE_ID))[1]
        row = await calendar_write.create(s, target, _draft())
        await s.commit()
        week = await calendar_view.week(s, SPACE_ID, date(2026, 9, 28))
    assert sent == {"account": "work", "calendar": "резерв", "uid": row.provider_uid,
                    "create": True}
    assert [p.event.title for d in week.days for p in d.events] == ["Созвон по смете"]
    assert calendar_write.participants_of(row) == ["anna@work.ru"]


async def test_what_niti_cannot_edit_says_why(db):
    async with db() as s:
        work = await _sbs(s)
        mine = CalendarEvent(account_id=work.id, provider_uid="x", calendar_name="резерв",
                             start_at=datetime(2026, 9, 1, tzinfo=UTC), attendees=[])
        assert calendar_write.editable(mine, work) is None
        repeat = CalendarEvent(account_id=work.id, provider_uid="x#2026-09-01",
                               calendar_name="резерв", attendees=[],
                               start_at=datetime(2026, 9, 1, tzinfo=UTC))
        theirs = CalendarEvent(account_id=work.id, provider_uid="y", calendar_name="резерв",
                               organizer="boss@corpmail.ru",
                               start_at=datetime(2026, 9, 1, tzinfo=UTC), attendees=[])
    assert "Повторяющуюся" in calendar_write.editable(repeat, work)
    assert "другой человек" in calendar_write.editable(theirs, work)


async def test_a_refused_write_changes_nothing_in_niti(db, monkeypatch):
    async def refuse(*a, **k):
        raise calendar_write.WriteError("Сервер календаря не принял событие: 403")

    monkeypatch.setattr(calendar_write, "_put", refuse)
    async with db() as s:
        await _sbs(s)
        target = (await calendar_write.targets(s, SPACE_ID))[0]
        with pytest.raises(calendar_write.WriteError):
            await calendar_write.create(s, target, _draft())
        titles = [e.title for e in (await s.execute(select(CalendarEvent))).scalars()]
    assert "Созвон по смете" not in titles


async def test_deleting_an_invited_meeting_quietly_silences_the_guests_first(db, monkeypatch):
    calls = []

    def fake_delete(base, login, password, calendar, uid, quiet_ical):
        calls.append((calendar, uid, quiet_ical))

    monkeypatch.setattr(calendar_write, "_delete_blocking", fake_delete)
    monkeypatch.setattr("email_reader.accounts.resolve_caldav_password", lambda a: "pw")
    async with db() as s:
        work = await _sbs(s)
        alone = CalendarEvent(account_id=work.id, provider_uid="a1", calendar_name="резерв",
                              start_at=datetime(2026, 10, 1, 7, tzinfo=UTC), attendees=[])
        guests = CalendarEvent(account_id=work.id, provider_uid="g1", calendar_name="резерв",
                               start_at=datetime(2026, 10, 1, 9, tzinfo=UTC),
                               end_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
                               organizer="me@corpmail.ru",
                               attendees=[{"email": "anna@work.ru", "name": None}])
        s.add_all([alone, guests])
        await s.flush()
        await calendar_write.delete(s, alone, notify=False)
        await calendar_write.delete(s, guests, notify=False)
        await calendar_write.delete(s, guests, notify=True)
    assert calls[0] == ("резерв", "a1", None)
    unfolded = calls[1][2].replace("\r\n ", "")   # длинные строки iCalendar переносятся
    assert "SCHEDULE-AGENT=CLIENT" in unfolded
    assert calls[2][2] is None
    assert alone.cancelled_at is not None and guests.cancelled_at is not None
