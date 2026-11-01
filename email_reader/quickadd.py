"""Quick-add: one typed line -> (title, due date).

Deterministic, Russian, no model: «позвонить Пете до пятницы» -> («позвонить Пете»,
next Friday). The same parser serves the board's quick-add box and the Telegram bot, so
a phrase means the same thing on both entrances. Weekdays resolve strictly AFTER today
(«в пятницу» said on a Friday is next week's), a bare «5 сентября» that already passed
is next year's.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# Nominative, accusative («в пятницу»), genitive («до пятницы»), dative («к пятнице»).
_WEEKDAYS = {
    "пн": 0, "понедельник": 0, "понедельника": 0, "понедельнику": 0,
    "вт": 1, "вторник": 1, "вторника": 1, "вторнику": 1,
    "ср": 2, "среда": 2, "среду": 2, "среды": 2, "среде": 2,
    "чт": 3, "четверг": 3, "четверга": 3, "четвергу": 3,
    "пт": 4, "пятница": 4, "пятницу": 4, "пятницы": 4, "пятнице": 4,
    "сб": 5, "суббота": 5, "субботу": 5, "субботы": 5, "субботе": 5,
    "вс": 6, "воскресенье": 6, "воскресенья": 6, "воскресенью": 6,
}
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_PREP = r"(?:до|к|на|в|во)"
_WD = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_MO = "|".join(_MONTHS)

# Each rule: compiled pattern -> function(match, today) -> date | None.
_RULES: list[tuple[re.Pattern, object]] = [
    (re.compile(r"(?<!\w)послезавтра(?!\w)", re.I), lambda m, t: t + timedelta(days=2)),
    (re.compile(r"(?<!\w)завтра(?!\w)", re.I), lambda m, t: t + timedelta(days=1)),
    (re.compile(r"(?<!\w)сегодня(?!\w)", re.I), lambda m, t: t),
    (re.compile(r"(?<!\w)через\s+недел[юи](?!\w)", re.I), lambda m, t: t + timedelta(days=7)),
    (
        re.compile(r"(?<!\w)через\s+(\d{1,3})\s+(?:день|дня|дней)(?!\w)", re.I),
        lambda m, t: t + timedelta(days=int(m.group(1))),
    ),
    (
        re.compile(rf"(?<!\w)(?:{_PREP}\s+)?({_WD})(?!\w)", re.I),
        lambda m, t: _next_weekday(t, _WEEKDAYS[m.group(1).lower()]),
    ),
    (
        re.compile(rf"(?<!\w)(?:{_PREP}\s+)?(\d{{1,2}})\s+({_MO})(?!\w)", re.I),
        lambda m, t: _day_month(t, int(m.group(1)), _MONTHS[m.group(2).lower()]),
    ),
    (
        re.compile(rf"(?<!\w)(?:{_PREP}\s+)?(\d{{1,2}})\.(\d{{1,2}})(?:\.(\d{{4}}|\d{{2}}))?(?!\w)"),
        lambda m, t: _numeric(t, m.group(1), m.group(2), m.group(3)),
    ),
]


def _next_weekday(today: date, wd: int) -> date:
    ahead = (wd - today.weekday()) % 7
    return today + timedelta(days=ahead or 7)


def _day_month(today: date, day: int, month: int) -> date | None:
    try:
        d = date(today.year, month, day)
    except ValueError:
        return None
    return d if d >= today else d.replace(year=today.year + 1)


def _numeric(today: date, d: str, m: str, y: str | None) -> date | None:
    try:
        if y:
            year = int(y) + (2000 if len(y) == 2 else 0)
            return date(year, int(m), int(d))
        return _day_month(today, int(d), int(m))
    except ValueError:
        return None


def parse(text: str, today: date) -> tuple[str, date | None]:
    """Split one line into a title and the date it names, if any. The date phrase is
    removed from the title; the first rule that matches wins."""
    raw = " ".join((text or "").split())
    for pattern, to_date in _RULES:
        m = pattern.search(raw)
        if not m:
            continue
        due = to_date(m, today)
        if due is None:
            continue
        title = (raw[: m.start()] + " " + raw[m.end():]).strip()
        title = re.sub(r"\s{2,}", " ", title)
        # A preposition left dangling at the end («позвонить до») is part of the date.
        title = re.sub(rf"\s+{_PREP}$", "", title, flags=re.I).strip(" ,.;:—-")
        return (title or raw), due
    return raw, None


def resolve(
    text: str, today: date, *, chosen: date | None = None, picked: bool = False
) -> tuple[str, date | None]:
    """Заголовок и срок для строки, у которой есть ещё и кнопка срока.

    Чей срок выигрывает: выбранный руками (`picked`) — всегда, включая выбор «Без срока»;
    иначе фраза в строке; предзаполнение доски (`chosen` без `picked`) — только когда фразы
    не было. Без этого «позвонить Пете до пятницы», набранное на «Сегодня», молча получало
    сегодняшний день: доска подставляла свой срок, а пользователь его не выбирал. Выбор
    руками сильнее фразы и без неоднозначности: кнопка показывает дату, пока строка пишется."""
    title, parsed = parse(text, today)
    if picked:
        return title, chosen
    return title, parsed or chosen
