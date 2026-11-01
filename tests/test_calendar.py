"""Календарь: раскладка недели по сетке, чтение из базы, панель события.

Пояс в раскладку передаётся явно: прогон идёт с TZ=UTC, и проверка, которая полагается
на системный пояс, здесь ничего бы не проверяла — а у владельца Москва.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from email_reader import calendar_grid as grid
from email_reader import calendar_view
from email_reader.models import Account, CalendarEvent, Meeting
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space

MSK = ZoneInfo("Europe/Moscow")
MONDAY = date(2026, 9, 21)


@dataclass
class Ev:
    start_at: datetime
    end_at: datetime | None
    title: str = "Встреча"
    cancelled_at: datetime | None = None


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


# --- раскладка --------------------------------------------------------------------------


def test_the_day_is_the_local_day_not_the_utc_one():
    """21:30 UTC понедельника — это 00:30 вторника по Москве."""
    late = Ev(_utc(21, 21, 30), _utc(21, 22, 30))
    moscow = grid.layout([late], MONDAY, tz=MSK)
    utc = grid.layout([late], MONDAY, tz=UTC)
    assert [len(d.events) for d in moscow.days][:2] == [0, 1]
    assert [len(d.events) for d in utc.days][:2] == [1, 0]
    assert moscow.days[1].events[0].top == 30          # 00:30 по Москве


def test_overlapping_meetings_share_the_day_and_the_next_one_gets_it_back():
    a = Ev(_utc(22, 7), _utc(22, 8))                   # 10:00–11:00 МСК
    b = Ev(_utc(22, 7, 30), _utc(22, 8, 30))            # 10:30–11:30
    c = Ev(_utc(22, 9), _utc(22, 10))                   # 12:00–13:00, ни с кем
    day = grid.layout([c, b, a], MONDAY, tz=MSK).days[1]
    placed = {p.event.start_at: p for p in day.events}
    assert (placed[a.start_at].col, placed[a.start_at].cols) == (0, 2)
    assert (placed[b.start_at].col, placed[b.start_at].cols) == (1, 2)
    assert (placed[c.start_at].col, placed[c.start_at].cols) == (0, 1)
    assert placed[b.start_at].left_pct == 50 and placed[b.start_at].width_pct == 50


def test_a_block_takes_the_free_columns_to_its_right():
    """Три встречи внахлёст делят день на три колонки, но только там, где они правда
    идут вместе: встреча в первой колонке, после которой справа пусто, растягивается."""
    long = Ev(_utc(22, 6), _utc(22, 9))                 # 09:00–12:00 МСК
    first = Ev(_utc(22, 6), _utc(22, 7))                # 09:00–10:00
    second = Ev(_utc(22, 6), _utc(22, 7))               # 09:00–10:00
    later = Ev(_utc(22, 8), _utc(22, 9))                # 11:00–12:00, рядом только long
    day = grid.layout([long, first, second, later], MONDAY, tz=MSK).days[1]
    by_top = sorted(day.events, key=lambda p: (p.top, p.col))
    assert all(p.cols == 3 for p in by_top)
    assert [p.span for p in by_top if p.top == 9 * 60] == [1, 1, 1]
    late = next(p for p in by_top if p.top == 11 * 60)
    assert (late.col, late.span, late.width_pct) == (1, 2, 200 / 3)


def test_empty_weekend_days_are_narrow_and_busy_ones_are_not():
    saturday = Ev(_utc(26, 7), _utc(26, 8))
    empty = grid.layout([], MONDAY, tz=MSK).columns.split()
    busy = grid.layout([saturday], MONDAY, tz=MSK).columns.split()
    assert empty[:5] == ["minmax(0,1fr)"] * 5 and empty[5:] == ["minmax(0,.4fr)"] * 2
    assert busy[5] == "minmax(0,1fr)" and busy[6] == "minmax(0,.4fr)"


def test_a_meeting_across_midnight_is_drawn_on_both_days():
    ev = Ev(_utc(22, 20), _utc(22, 22))                 # 23:00–01:00 МСК
    days = grid.layout([ev], MONDAY, tz=MSK).days
    first, second = days[1].events[0], days[2].events[0]
    assert first.to_tomorrow and not first.from_yesterday
    assert second.from_yesterday and second.top == 0 and second.height == 60


def test_ending_exactly_at_midnight_does_not_leave_an_empty_piece_tomorrow():
    ev = Ev(_utc(22, 20), _utc(22, 21))                 # 23:00–00:00 МСК
    days = grid.layout([ev], MONDAY, tz=MSK).days
    assert len(days[1].events) == 1 and days[2].events == []


def test_a_short_or_endless_meeting_is_still_something_to_click():
    tiny = Ev(_utc(23, 7), _utc(23, 7, 5))
    endless = Ev(_utc(24, 7), None)
    days = grid.layout([tiny, endless], MONDAY, tz=MSK).days
    assert days[2].events[0].height == grid.MIN_MINUTES
    assert days[3].events[0].height == grid.NO_END_MINUTES


def test_an_all_day_event_goes_to_the_strip_not_to_three_in_the_morning():
    """CalDAV хранит дату как полночь UTC; по Москве это 03:00."""
    holiday = Ev(datetime(2026, 9, 23, tzinfo=UTC), datetime(2026, 9, 25, tzinfo=UTC))
    week = grid.layout([holiday], MONDAY, tz=MSK)
    assert week.has_all_day
    assert [d.date.day for d in week.days if d.all_day] == [23, 24]
    assert all(not d.events for d in week.days)


def test_the_grid_opens_at_eight_unless_something_starts_earlier():
    usual = Ev(_utc(22, 7), _utc(22, 8))                # 10:00
    early = Ev(_utc(23, 3, 30), _utc(23, 4))            # 06:30
    assert grid.layout([usual], MONDAY, tz=MSK).first_hour == 8
    assert grid.layout([usual, early], MONDAY, tz=MSK).first_hour == 6


def test_now_is_marked_only_on_the_week_that_contains_today():
    now = _utc(22, 9)                                   # вторник 12:00 МСК
    this = grid.layout([], MONDAY, tz=MSK, now=now)
    other = grid.layout([], MONDAY + timedelta(days=7), tz=MSK, now=now)
    assert this.days[1].today and this.now_pct == 50
    assert other.now_pct is None and not any(d.today for d in other.days)


def test_a_month_cell_puts_all_day_first_and_cancelled_last():
    """В клетке месяца три строки: отменённая встреча не должна занимать место живой."""
    cancelled = Ev(_utc(22, 6), _utc(22, 7), title="Отменили", cancelled_at=_utc(21, 9))
    live = Ev(_utc(22, 8), _utc(22, 9), title="Живая")
    holiday = Ev(datetime(2026, 9, 22, tzinfo=UTC), datetime(2026, 9, 23, tzinfo=UTC),
                 title="Праздник")
    late = Ev(_utc(22, 20), _utc(22, 22), title="До ночи")    # 23:00–01:00 МСК
    month = grid.month_layout([cancelled, live, holiday, late], date(2026, 9, 1), tz=MSK)
    cells = {d.date: d for w in month.weeks for d in w}
    assert [e.title for e in cells[date(2026, 9, 22)].items] == [
        "Праздник", "Живая", "До ночи", "Отменили"]
    assert [e.title for e in cells[date(2026, 9, 23)].items] == ["До ночи"]


def test_week_bounds_are_local_midnights():
    lo, hi = grid.bounds_utc(MONDAY, MSK)
    assert lo == datetime(2026, 9, 20, 21, tzinfo=UTC)
    assert hi == datetime(2026, 9, 27, 21, tzinfo=UTC)


# --- база -------------------------------------------------------------------------------


async def _two_spaces(s):
    await mk_space(s, 1, name="Работа")
    await mk_space(s, 2, name="Личное")
    work = Account(space_id=SPACE_ID, name="w", host="h", login="me@work.ru", folders=[])
    home = Account(space_id=SPACE_ID_2, name="p", host="h", login="me@home.ru", folders=[])
    s.add_all([work, home])
    await s.flush()
    return work, home


def _event(account, uid, start, end, **kw):
    return CalendarEvent(account_id=account.id, provider_uid=uid, start_at=start,
                         end_at=end, **kw)


async def test_the_week_shows_this_space_only_and_keeps_cancelled_meetings(db):
    async with db() as s:
        work, home = await _two_spaces(s)
        s.add_all([
            _event(work, "a", _utc(22, 7), _utc(22, 8), title="Планёрка"),
            _event(work, "b", _utc(23, 7), _utc(23, 8), title="Отменили",
                   cancelled_at=_utc(20, 10)),
            _event(home, "c", _utc(22, 7), _utc(22, 8), title="Стоматолог"),
            # воскресенье 22:00 UTC — это уже понедельник 01:00 следующей недели по Москве
            _event(work, "d", _utc(27, 22), _utc(27, 23), title="Следующая неделя"),
        ])
        await s.commit()
        week = await calendar_view.week(s, SPACE_ID, MONDAY, tz=MSK)
    titles = [p.event.title for d in week.days for p in d.events]
    assert titles == ["Планёрка", "Отменили"]


async def test_an_event_of_another_space_cannot_be_opened(db):
    async with db() as s:
        work, home = await _two_spaces(s)
        ev = _event(home, "c", _utc(22, 7), _utc(22, 8), title="Стоматолог")
        s.add(ev)
        await s.commit()
        assert await calendar_view.get_event(s, ev.id, SPACE_ID) is None
        assert (await calendar_view.get_event(s, ev.id, SPACE_ID_2)).title == "Стоматолог"


async def test_the_recording_of_the_meeting_is_found_by_time(db):
    async with db() as s:
        work, home = await _two_spaces(s)
        ev = _event(work, "a", _utc(22, 7), _utc(22, 8), title="Планёрка")
        rec = Meeting(ext_id=f"m-{uuid.uuid4().hex}", space_id=SPACE_ID, title="Планёрка",
                      started_at=_utc(22, 7, 3), duration_sec=3000)
        other_space = Meeting(ext_id=f"m-{uuid.uuid4().hex}", space_id=SPACE_ID_2,
                              title="Чужая", started_at=_utc(22, 7), duration_sec=3600)
        later = Meeting(ext_id=f"m-{uuid.uuid4().hex}", space_id=SPACE_ID, title="Потом",
                        started_at=_utc(22, 10), duration_sec=1800)
        s.add_all([ev, rec, other_space, later])
        await s.commit()
        found = await calendar_view.recordings_for(s, ev, SPACE_ID)
    assert [m.title for m in found] == ["Планёрка"]


# --- экран ------------------------------------------------------------------------------


def _render(name: str, **ctx) -> str:
    import email_reader.api.app  # noqa: F401 — регистрирует icon() и фильтры
    from email_reader.api.common import templates

    return templates.env.get_template(name).render(**ctx)


async def _render_calendar(s, space_id, view, anchor, hidden=frozenset()):  # noqa: ANN001
    from email_reader.api.routers import calendar as route
    from email_reader.models import Space

    ctx = await route._ctx(s, await s.get(Space, space_id), view, anchor, hidden)
    return _render("_calendar.html", **ctx), ctx


async def test_the_week_page_draws_blocks_that_open_the_event(db):
    async with db() as s:
        work, _home = await _two_spaces(s)
        ev = _event(work, "a", _utc(22, 7), _utc(22, 8), title="Планёрка",
                    location="Переговорная 5")
        s.add(ev)
        await s.commit()
        html, _ctx = await _render_calendar(s, SPACE_ID, "week", MONDAY)
    assert f'hx-get="/event/{ev.id}"' in html
    assert "Планёрка" in html and "Переговорная 5" in html and "--top:" in html
    assert "Сентябрь 2026" in html
    # Шапка как в Календаре: три вида, стрелки и «Сегодня» — с адресом в строке окна.
    assert 'hx-push-url="/calendar?view=day&amp;date=2026-09-21"' in html
    assert 'hx-push-url="/calendar?view=week&amp;date=2026-09-14"' in html
    assert 'data-cal-key="month"' in html and 'data-cal-key="today"' in html
    # Цвет календаря ящика — в блоке, и сам ящик — галочкой над сеткой.
    assert "--ev:var(--cal-1)" in html and "cal-sources" in html
    assert "встреч нет" not in html


def test_a_longer_meeting_shows_more_lines_of_its_title():
    """Строк темы — сколько влезает под временем: иначе у края блока торчит половина строки."""
    def lines(minutes: int) -> int:
        start = _utc(21, 9)  # понедельник — первый день недели
        week = grid.layout([Ev(start, start + timedelta(minutes=minutes))], MONDAY, tz=UTC)
        return week.days[0].events[0].title_lines

    assert lines(20) == lines(45) == lines(60) == 1
    assert lines(90) < lines(120) < lines(180)


def test_a_narrow_block_gives_the_time_line_to_the_title():
    """В узком блоке время прячется (его видно по сетке) — тема получает эту строку."""
    start = _utc(21, 9)
    week = grid.layout([Ev(start, start + timedelta(hours=1))], MONDAY, tz=UTC)
    hour = week.days[0].events[0]
    assert (hour.title_lines, hour.title_lines_alone) == (1, 2)


async def test_a_block_says_when_it_starts_and_then_what_it_is(db):
    """В узкой колонке «14:30–15:30» переносилось на две строки и забирало место темы —
    от неё оставалась «Д…». Конец виден по сетке; в блоке — начало и под ним тема."""
    async with db() as s:
        work, _home = await _two_spaces(s)
        s.add(_event(work, "a", _utc(22, 11, 30), _utc(22, 12, 30), title="Демо Альфа"))
        await s.commit()
        html, _ctx = await _render_calendar(s, SPACE_ID, "week", MONDAY)

    start, end = (_utc(22, 11, 30).astimezone().strftime("%H:%M"),
                  _utc(22, 12, 30).astimezone().strftime("%H:%M"))
    block = html[html.index('class="cal-ev'):]
    block = block[:block.index("</button>")]
    body = block[block.index("cal-ev-body"):]
    assert body.index("cal-ev-time") < body.index("Демо Альфа")
    assert f">{start}</span>" in body and f"{start}–{end}" not in body
    assert "--lines:1;--lines-alone:2" in block
    assert f'title="{start}–{end} · Демо Альфа"' in block


async def test_an_empty_week_says_so_instead_of_a_blank_grid(db):
    async with db() as s:
        await _two_spaces(s)
        html, _ctx = await _render_calendar(s, SPACE_ID_2, "week", MONDAY)
    assert "На этой неделе" in html and "в «Личное» встреч нет" in html


async def test_a_hidden_calendar_leaves_the_grid_and_its_chip_goes_off(db):
    async with db() as s:
        work, _home = await _two_spaces(s)
        s.add(_event(work, "a", _utc(22, 7), _utc(22, 8), title="Планёрка"))
        await s.commit()
        key = calendar_view.source_key(work.id, None)
        html, ctx = await _render_calendar(s, SPACE_ID, "week", MONDAY, frozenset({key}))
    assert "Планёрка" not in html.split("cal-grid", 1)[1]
    assert 'class="proj-chip" type="button"' in html        # галочка снята
    assert "календари спрятаны" in html
    assert ctx["toggle"](key) == ""                          # щелчок вернёт календарь


async def test_each_calendar_of_a_mailbox_has_its_own_colour_and_switch(db):
    """У ящика work три календаря — «Мои события», «резерв», «Не забыть»: раньше все
    красились цветом ящика."""
    async with db() as s:
        work, _home = await _two_spaces(s)
        s.add_all([
            _event(work, "a", _utc(22, 7), _utc(22, 8), title="Планёрка",
                   calendar_name="Мои события", created_at=_utc(1, 0)),
            _event(work, "b", _utc(22, 10), _utc(22, 11), title="резерв",
                   calendar_name="резерв", created_at=_utc(2, 0)),
            _event(work, "c", _utc(22, 12), _utc(22, 13), title="Удалённая",
                   created_at=_utc(3, 0)),
        ])
        await s.commit()
        sources = await calendar_view.calendars(s, SPACE_ID)
        busy = calendar_view.source_key(work.id, "резерв")
        html, _ctx = await _render_calendar(s, SPACE_ID, "week", MONDAY)
        hidden, _ctx = await _render_calendar(s, SPACE_ID, "week", MONDAY, frozenset({busy}))
    assert [(c.label, c.color) for c in sources] == [
        ("Мои события", 1), ("резерв", 2), ("w · прочие", 3)]
    assert "--ev:var(--cal-1)" in html and "--ev:var(--cal-2)" in html
    grid = hidden.split("cal-grid", 1)[1]
    assert "Планёрка" in grid and "резерв" not in grid


def test_old_links_with_a_mailbox_id_in_hide_do_not_break_the_page():
    from email_reader.api.routers.calendar import _hidden

    assert _hidden(f"{uuid.uuid4()},abcdef01") == frozenset({"abcdef01"})


async def test_the_day_view_is_one_wide_column(db):
    async with db() as s:
        work, _home = await _two_spaces(s)
        s.add(_event(work, "a", _utc(22, 7), _utc(22, 8), title="Планёрка"))
        await s.commit()
        html, ctx = await _render_calendar(s, SPACE_ID, "day", date(2026, 9, 22))
    assert len(ctx["week"].days) == 1 and ctx["week"].columns == "minmax(0,1fr)"
    assert "Вторник, 22 сентября 2026" in html and "Планёрка" in html
    assert (ctx["prev"], ctx["next"]) == (date(2026, 9, 21), date(2026, 9, 23))


async def test_the_month_shows_three_meetings_a_day_and_the_rest_as_more(db):
    async with db() as s:
        work, _home = await _two_spaces(s)
        s.add_all([_event(work, str(i), _utc(22, 6 + i), _utc(22, 7 + i), title=f"Встреча {i}")
                   for i in range(5)])
        await s.commit()
        html, ctx = await _render_calendar(s, SPACE_ID, "month", date(2026, 9, 15))
    weeks = ctx["month"].weeks
    assert weeks[0][0].date == date(2026, 8, 31) and not weeks[0][0].in_month
    assert weeks[-1][-1].date == date(2026, 10, 4)
    tuesday = next(d for w in weeks for d in w if d.date == date(2026, 9, 22))
    assert [e.title for e in tuesday.shown] == ["Встреча 0", "Встреча 1", "Встреча 2"]
    assert tuesday.more == 2 and "ещё 2" in html
    assert (ctx["prev"], ctx["next"]) == (date(2026, 8, 1), date(2026, 10, 1))


def test_the_title_reads_like_the_calendar():
    from email_reader.api.routers.calendar import title

    assert title("week", date(2026, 9, 23)) == "Сентябрь 2026"
    assert title("week", date(2026, 9, 30)) == "Сентябрь – Октябрь 2026"
    assert title("week", date(2026, 12, 30)) == "Декабрь 2026 – Январь 2027"
    assert title("month", date(2026, 9, 23)) == "Сентябрь 2026"
    assert title("day", date(2026, 9, 23)) == "Среда, 23 сентября 2026"


def test_the_mini_month_marks_what_is_shown_and_keeps_the_view():
    from email_reader.api.routers import calendar as route

    ctx = route._nav("day", date(2026, 9, 23), frozenset())
    ctx.update(route._mini(ctx["anchor"], date(2026, 9, 1), route._shown("day", ctx["anchor"])))
    html = _render("_calmini.html", **ctx)
    assert "Сентябрь 2026" in html
    assert 'hx-get="/calendar/mini?month=2026-10-01&view=day&amp;date=2026-09-23"' in html
    assert html.count(" shown") == 1                       # показан один день
    assert 'hx-push-url="/calendar?view=day&amp;date=2026-09-24"' in html


async def test_the_event_pane_says_what_when_where_who_and_links_the_recording(db):
    from email_reader.api.routers.calendar import MONTHS, WEEKDAYS

    async with db() as s:
        work, _home = await _two_spaces(s)
        ev = _event(work, "a", _utc(22, 7), _utc(22, 8), title="Планёрка",
                    location="Переговорная 5", organizer="boss@work.ru",
                    attendees=[{"email": "boss@work.ru", "name": "Босс"},
                               {"email": "anna@work.ru", "name": "Анна"}],
                    description="Повестка:\n1. Смета", cancelled_at=_utc(21, 9))
        rec = Meeting(ext_id=f"m-{uuid.uuid4().hex}", space_id=SPACE_ID, title="Планёрка",
                      started_at=_utc(22, 7), duration_sec=3600)
        s.add_all([ev, rec])
        await s.commit()
        html = _render("_eventpane.html", event=ev, mailbox=work,
                       recordings=await calendar_view.recordings_for(s, ev, SPACE_ID),
                       all_day=False, MONTHS=MONTHS, WEEKDAYS=WEEKDAYS)
    assert "Переговорная 5" in html and "Отменена" in html
    assert 'hx-get="/person?q=anna%40work.ru"' in html
    # Организатор один раз, хоть он и в списке участников: второго чипа «Босс» нет.
    assert html.count("boss%40work.ru") == 1 and "Босс" not in html
    assert f'hx-get="/meeting/{rec.id}"' in html
    assert "Повестка:\n1. Смета" in html


def test_the_calendar_has_its_routes_and_a_search_hit_opens_the_pane():
    from email_reader.api.app import app
    from email_reader.search.query import _url

    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert {"/calendar", "/calendar/grid", "/calendar/mini", "/event/{event_id}"} <= paths
    ref = uuid.uuid4().hex
    assert _url("event", ref, None) == (f"/event/{uuid.UUID(hex=ref)}", True)


def test_the_calendar_is_fetched_far_enough_for_the_month_view():
    """С горизонтом в неделю «Месяц» был пуст дальше семи дней вперёд."""
    from email_reader import calendar_sync

    assert calendar_sync.DEFAULT_HORIZON_DAYS >= 62


async def test_caldav_sync_remembers_which_calendar_an_event_came_from(db, monkeypatch):
    """Имя календаря доходит от сервера до строки — и у новой встречи, и у уже
    лежащей в базе (так старые встречи узнают свой календарь при следующей сверке)."""
    import sys
    import types

    from email_reader import calendar_sync
    from email_reader.connectors import caldav

    ical = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{uid}\r\n"
            "DTSTART:20260922T070000Z\r\nDTEND:20260922T080000Z\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n")

    class _Cal:
        def __init__(self, name, uid):
            self.name, self.uid = name, uid

        def search(self, **_kw):
            return [types.SimpleNamespace(data=ical.format(uid=self.uid))]

    class _Client:
        def __init__(self, **_kw): ...
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def principal(self):
            return types.SimpleNamespace(
                calendars=lambda: [_Cal("Мои события", "a"), _Cal("резерв", "b")])

    monkeypatch.setitem(sys.modules, "caldav", types.SimpleNamespace(DAVClient=_Client))
    fetched, complete = caldav._fetch_blocking("u", "l", "p", _utc(21, 0), _utc(28, 0))
    assert complete and {(f.provider_uid, f.calendar) for f in fetched} == {
        ("a", "Мои события"), ("b", "резерв")}

    async with db() as s:
        work, _home = await _two_spaces(s)
        s.add(_event(work, "b", _utc(22, 7), _utc(22, 8), title="b"))  # до миграции
        await s.flush()
        await calendar_sync.store_events(s, work.id, fetched)
        rows = {e.provider_uid: e.calendar_name for e in
                (await s.execute(select(CalendarEvent))).scalars()}
    assert rows == {"a": "Мои события", "b": "резерв"}


async def test_past_meetings_learn_their_calendar_once(db, monkeypatch):
    from email_reader import calendar_sync
    from email_reader.connectors import caldav

    calls = []

    async def fake_fetch(_base, _login, _pw, start, end):
        calls.append((start, end))
        return [caldav.FetchedEvent("old", _utc(21, 7), _utc(21, 8), "old", None, None,
                                    None, calendar="резерв")], True

    monkeypatch.setattr(caldav, "fetch_events", fake_fetch)
    async with db() as s:
        work, _home = await _two_spaces(s)
        s.add_all([_event(work, "old", _utc(21, 7), _utc(21, 8), title="old"),
                   _event(work, "gone", _utc(21, 9), _utc(21, 10), title="gone")])
        await s.flush()
        first = await calendar_sync.name_past_events(s, work, "u", "p", _utc(24, 9))
        again = await calendar_sync.name_past_events(s, work, "u", "p", _utc(24, 9))
        rows = {e.provider_uid: e.calendar_name for e in
                (await s.execute(select(CalendarEvent))).scalars()}
    assert (first, again, len(calls)) == (1, 0, 1)
    assert rows == {"old": "резерв", "gone": None}
