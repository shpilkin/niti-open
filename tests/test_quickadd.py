"""Quick-add parser: a typed line -> (title, due). TODAY is a Friday (2026-09-04)."""

from __future__ import annotations

from datetime import date

import pytest

from email_reader.quickadd import parse

TODAY = date(2026, 9, 4)  # Friday


@pytest.mark.parametrize(
    ("text", "title", "due"),
    [
        ("позвонить Пете до пятницы", "позвонить Пете", date(2026, 9, 11)),   # strictly after today
        ("в пт созвон", "созвон", date(2026, 9, 11)),
        ("отчёт к понедельнику", "отчёт", date(2026, 9, 7)),
        ("отчёт в понедельник", "отчёт", date(2026, 9, 7)),
        ("продлить домен завтра", "продлить домен", date(2026, 9, 5)),
        ("сегодня оплатить счёт", "оплатить счёт", date(2026, 9, 4)),
        ("послезавтра забрать посылку", "забрать посылку", date(2026, 9, 6)),
        ("через 3 дня написать Ане", "написать Ане", date(2026, 9, 7)),
        ("через неделю проверить", "проверить", date(2026, 9, 11)),
        ("сдать смету до 30 сентября", "сдать смету", date(2026, 9, 30)),
        # Already passed this year -> next year.
        ("сдать смету 1 сентября", "сдать смету", date(2027, 9, 1)),
        ("подписать акт до 15.10", "подписать акт", date(2026, 10, 15)),
        ("подписать акт 15.10.2027", "подписать акт", date(2027, 10, 15)),
        ("просто задача", "просто задача", None),
        ("  много   пробелов  ", "много пробелов", None),
        # Only a date -> the word itself stays as the title.
        ("завтра", "завтра", date(2026, 9, 5)),
    ],
)
def test_parse(text, title, due):
    assert parse(text, TODAY) == (title, due)


def test_impossible_dates_are_left_in_the_title():
    assert parse("31 февраля разобрать", TODAY) == ("31 февраля разобрать", None)
    assert parse("встреча 32.13", TODAY) == ("встреча 32.13", None)
