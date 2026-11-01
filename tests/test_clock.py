"""Календарный день пользователя: он местный, а не «сутки UTC»."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime

import pytest

from email_reader import clock


@pytest.fixture
def moscow(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_the_day_turns_at_local_midnight_not_at_utc_midnight(moscow):
    """01:30 в Москве — это уже новый день, а по UTC ещё старый. Пока «сегодня» считали
    от UTC, первые три часа суток срок на сегодня показывался как «завтра»."""
    just_past_local_midnight = datetime(2026, 9, 11, 22, 30, tzinfo=UTC)
    assert just_past_local_midnight.date() == date(2026, 9, 11)          # так было
    assert clock.today(just_past_local_midnight) == date(2026, 9, 12)    # так верно


def test_without_a_moment_it_is_the_current_local_day(moscow):
    assert clock.today() == datetime.now().astimezone().date()
