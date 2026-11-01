"""Неделя календаря сеткой: в какой день событие, на какой высоте, в какой колонке.

Чистые функции, без базы. Часовой пояс передаётся явно: событие хранится в UTC, а день и
высота в сетке — местные. Встреча в 21:30 UTC понедельника по Москве стоит во вторнике в
00:30, и раскладка, которая молча берёт системный пояс, в тестах (они идут с TZ=UTC)
ничего бы не проверяла. `tz=None` значит «системный пояс» — так рисует и фильтр
`local` в шаблонах, с переходом на летнее время для каждого момента отдельно.

Встречи внахлёст делят ширину дня на колонки. Считается здесь, на сервере: браузер
получает готовые проценты и ничего не пересчитывает.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

DAY_MINUTES = 24 * 60
# Самое короткое, что рисуем: иначе встречу на пять минут не видно и не нажать.
MIN_MINUTES = 20
# Событие без конца — на полчаса, как рисует его Calendar.app.
NO_END_MINUTES = 30
# Куда прокрутить сетку, если на неделе нет ничего раньше.
DEFAULT_FIRST_HOUR = 8
# Размеры блока встречи из app.css: час сетки (--cal-hour-h), поля блока, строка времени и
# строка темы — по ним считается, сколько строк темы влезает.
HOUR_PX, PAD_PX, TIME_PX, LINE_PX = 44, 4, 14, 16


@dataclass
class Placed:
    """Кусок события внутри одного дня. Событие через полночь даёт два куска."""

    event: Any
    start: datetime          # местное, обрезано границами дня
    end: datetime
    top: int                 # минут от начала дня
    height: int              # минут, не меньше MIN_MINUTES
    col: int = 0
    cols: int = 1
    span: int = 1            # сколько колонок занимает: свободные справа — тоже его
    from_yesterday: bool = False
    to_tomorrow: bool = False

    @property
    def left_pct(self) -> float:
        return 100 * self.col / self.cols

    @property
    def width_pct(self) -> float:
        return 100 * self.span / self.cols

    @property
    def top_pct(self) -> float:
        return 100 * self.top / DAY_MINUTES

    @property
    def height_pct(self) -> float:
        return 100 * self.height / DAY_MINUTES

    @property
    def title_lines(self) -> int:
        """Сколько строк темы влезает под временем: последняя обрезается многоточием, а не
        половиной строки у края блока."""
        return max(1, int((self.height * HOUR_PX / 60 - PAD_PX - TIME_PX) // LINE_PX))

    @property
    def title_lines_alone(self) -> int:
        """То же без строки времени — в узком блоке время прячется, тема забирает строку."""
        return max(1, int((self.height * HOUR_PX / 60 - PAD_PX) // LINE_PX))


@dataclass
class Day:
    date: date
    today: bool
    events: list[Placed] = field(default_factory=list)
    all_day: list[Any] = field(default_factory=list)

    @property
    def weekend(self) -> bool:
        return self.date.weekday() >= 5


@dataclass
class Week:
    start: date
    days: list[Day]
    now_pct: float | None    # линия «сейчас», если сегодня на этой неделе
    first_hour: int          # к какому часу прокрутить при открытии
    now_label: str = ""      # «22:35» — подпись линии «сейчас» на шкале часов

    @property
    def end(self) -> date:
        return self.start + timedelta(days=len(self.days) - 1)

    @property
    def has_all_day(self) -> bool:
        return any(d.all_day for d in self.days)

    @property
    def columns(self) -> str:
        """Ширины столбцов для CSS grid. Пустая суббота или воскресенье — узкие: на
        рабочей неделе им нечего показывать, а место нужно встречам внахлёст."""
        narrow = len(self.days) == 7
        return " ".join(
            "minmax(0,.4fr)" if narrow and d.weekend and not d.events and not d.all_day
            else "minmax(0,1fr)"
            for d in self.days
        )


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _local(dt: datetime, tz: tzinfo | None) -> datetime:
    return dt.astimezone(tz) if tz is not None else dt.astimezone()


def _midnight(day: date, tz: tzinfo | None) -> datetime:
    if tz is not None:
        return datetime.combine(day, time.min, tzinfo=tz)
    return datetime.combine(day, time.min).astimezone()


def bounds_utc(first: date, tz: tzinfo | None, days: int = 7) -> tuple[datetime, datetime]:
    """Отрезок в UTC для запроса: с местной полуночи `first` на `days` дней вперёд."""
    return (_midnight(first, tz).astimezone(UTC),
            _midnight(first + timedelta(days=days), tz).astimezone(UTC))


def is_all_day(start_at: datetime, end_at: datetime | None) -> bool:
    """Событие «на весь день» в той записи, в какой его сейчас хранит CalDAV.

    `caldav._to_utc` кладёт дату как полночь UTC; в сетке такое событие встало бы на
    03:00 по Москве. Пока колонки `all_day` нет, узнаём его по этой записи: начало ровно
    в полночь UTC и длина — целые сутки (docs/feature-calendar.md §7).
    """
    if end_at is None:
        return False
    start = start_at.astimezone(UTC)
    span = end_at - start_at
    return (start.time() == time.min and span >= timedelta(days=1)
            and span % timedelta(days=1) == timedelta(0))


def _all_day_dates(ev: Any) -> list[date]:
    """Даты события на весь день — по UTC: это дата, а не момент, и пояс её не сдвигает."""
    first = ev.start_at.astimezone(UTC).date()
    days = (ev.end_at - ev.start_at).days
    return [first + timedelta(days=i) for i in range(days)]


def _columns(items: list[Placed]) -> None:
    """Раскладка внахлёст: группы пересекающихся событий, в группе — колонки."""
    group: list[Placed] = []
    group_end = -1

    def close() -> None:
        ends: list[int] = []
        for it in group:
            for i, e in enumerate(ends):
                if e <= it.top:
                    it.col, ends[i] = i, it.top + it.height
                    break
            else:
                it.col = len(ends)
                ends.append(it.top + it.height)
        for it in group:
            it.cols = len(ends)
        # Колонки нужны только там, где встречи правда идут одновременно. Блок, у
        # которого справа в его время пусто, растягивается на это пустое место: на
        # живой неделе половина блоков иначе была уже 45 пикселей и не читалась.
        for it in group:
            lo, hi = it.top, it.top + it.height
            while it.col + it.span < it.cols and not any(
                o.col == it.col + it.span and o.top < hi and o.top + o.height > lo
                for o in group
            ):
                it.span += 1

    for it in sorted(items, key=lambda p: (p.top, -p.height)):
        if group and it.top >= group_end:
            close()
            group, group_end = [], -1
        group.append(it)
        group_end = max(group_end, it.top + it.height)
    if group:
        close()


def layout(events: list[Any], week_start: date, *, days_count: int = 7,
           tz: tzinfo | None = None, now: datetime | None = None) -> Week:
    """Разложить события по дням, начиная с `week_start`: семь дней — неделя, один — день.

    Событие — любой объект с `start_at`, `end_at` (UTC, aware; конец может быть None).
    """
    days = [Day(date=week_start + timedelta(days=i), today=False)
            for i in range(days_count)]
    by_date = {d.date: d for d in days}
    now_local = _local(now, tz) if now is not None else None
    if now_local is not None and now_local.date() in by_date:
        by_date[now_local.date()].today = True

    for ev in events:
        if is_all_day(ev.start_at, ev.end_at):
            for d in _all_day_dates(ev):
                if d in by_date:
                    by_date[d].all_day.append(ev)
            continue
        start = _local(ev.start_at, tz)
        end = (_local(ev.end_at, tz) if ev.end_at is not None and ev.end_at > ev.start_at
               else start + timedelta(minutes=NO_END_MINUTES))
        day = start.date()
        while day <= end.date():
            if day in by_date:
                day_start = _midnight(day, tz)
                lo, hi = max(start, day_start), min(end, _midnight(day + timedelta(days=1), tz))
                if hi > lo:  # конец ровно в полночь не заводит пустой кусок на завтра
                    top = int((lo - day_start).total_seconds() // 60)
                    minutes = int((hi - lo).total_seconds() // 60)
                    by_date[day].events.append(Placed(
                        event=ev, start=lo, end=hi, top=top,
                        height=max(MIN_MINUTES, min(minutes, DAY_MINUTES - top)),
                        from_yesterday=lo > start, to_tomorrow=hi < end,
                    ))
            day += timedelta(days=1)

    for d in days:
        _columns(d.events)
        d.events.sort(key=lambda p: (p.top, p.col))
        d.all_day.sort(key=lambda e: e.title or "")

    earliest = [p.top // 60 for d in days for p in d.events if not p.from_yesterday]
    first_hour = min([DEFAULT_FIRST_HOUR, *earliest])
    now_pct, now_label = None, ""
    if now_local is not None and now_local.date() in by_date:
        minutes = now_local.hour * 60 + now_local.minute
        now_pct = 100 * minutes / DAY_MINUTES
        now_label = now_local.strftime("%H:%M")
    return Week(start=week_start, days=days, now_pct=now_pct, first_hour=first_hour,
                now_label=now_label)


# --- месяц --------------------------------------------------------------------------------

# Сколько встреч видно в клетке месяца; остальное — «ещё N», как в Календаре.
MONTH_LINES = 3


@dataclass
class MonthDay:
    date: date
    in_month: bool
    today: bool
    items: list[Any] = field(default_factory=list)   # сначала «весь день», потом по времени

    @property
    def shown(self) -> list[Any]:
        return self.items[:MONTH_LINES]

    @property
    def more(self) -> int:
        return max(0, len(self.items) - MONTH_LINES)


@dataclass
class Month:
    first: date                    # первое число месяца
    weeks: list[list[MonthDay]]


def month_bounds(first: date) -> tuple[date, date]:
    """Первый понедельник сетки и день после её последнего воскресенья."""
    start = monday_of(first)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    end = monday_of(last) + timedelta(days=7)
    return start, end


def month_layout(events: list[Any], first: date, *, tz: tzinfo | None = None,
                 now: datetime | None = None) -> Month:
    """Месяц сеткой недель с понедельника. Событие через полночь стоит в каждом своём дне."""
    first = first.replace(day=1)
    start, end = month_bounds(first)
    today = _local(now, tz).date() if now is not None else None
    cells = {start + timedelta(days=i): MonthDay(
        date=start + timedelta(days=i), in_month=(start + timedelta(days=i)).month == first.month,
        today=start + timedelta(days=i) == today,
    ) for i in range((end - start).days)}
    for ev in events:
        if is_all_day(ev.start_at, ev.end_at):
            for d in _all_day_dates(ev):
                if d in cells:
                    cells[d].items.append(ev)
            continue
        s = _local(ev.start_at, tz)
        e = _local(ev.end_at, tz) if ev.end_at and ev.end_at > ev.start_at else s
        day = s.date()
        while day <= e.date():
            if day in cells and (day == s.date() or e > _midnight(day, tz)):
                cells[day].items.append(ev)
            day += timedelta(days=1)

    def order(ev: Any) -> tuple:
        # В клетке три строки: отменённые — после живых, иначе они съедают место встреч,
        # которые правда будут.
        return (not is_all_day(ev.start_at, ev.end_at),
                getattr(ev, "cancelled_at", None) is not None, ev.start_at)

    for cell in cells.values():
        cell.items.sort(key=order)
    ordered = [cells[k] for k in sorted(cells)]
    return Month(first=first, weeks=[ordered[i:i + 7] for i in range(0, len(ordered), 7)])
