"""Календарный день пользователя.

`due_date`, `next_check` и прочие даты в БД — это дни местного календаря, а не сутки
UTC. Сравнивать их с `datetime.now(UTC).date()` нельзя: в MSK (UTC+3) с полуночи до
03:00 такая «сегодня» — это ещё вчера, и срок на сегодня показывается как «завтра».
"""

from __future__ import annotations

from datetime import UTC, date, datetime


def today(now: datetime | None = None) -> date:
    """Сегодняшняя дата в часовом поясе системы; `now` — опорный момент вместо текущего."""
    return (now or datetime.now(UTC)).astimezone().date()
