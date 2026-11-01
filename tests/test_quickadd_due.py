"""Срок у новой задачи: строка, кнопка и предзаполнение доски (docs/ux-tasks.md §17.7).

Кнопка срока появилась потому, что набранная задача без срока уезжала на «Мои задачи» в
самый низ — на доску, которую пользователь не открывал. Три источника даты и порядок между
ними проверяются здесь; что карточку видно после добавления — в e2e.
"""

from __future__ import annotations

from datetime import date

from email_reader import quickadd

TODAY = date(2026, 9, 14)  # понедельник


def test_a_typed_phrase_still_wins_when_nothing_was_chosen():
    title, due = quickadd.resolve("позвонить Пете до пятницы", TODAY)
    assert title == "позвонить Пете"
    assert due == date(2026, 9, 18)


def test_a_chosen_date_overrides_the_typed_phrase():
    title, due = quickadd.resolve(
        "позвонить Пете до пятницы", TODAY, chosen=date(2026, 9, 16), picked=True
    )
    assert title == "позвонить Пете"
    assert due == date(2026, 9, 16)


def test_the_boards_prefill_does_not_override_a_typed_phrase():
    """«Сегодня» подставляет сегодняшний день, но пользователь его не выбирал: набранная
    в строке пятница сильнее."""
    _, due = quickadd.resolve(
        "позвонить Пете до пятницы", TODAY, chosen=TODAY, picked=False
    )
    assert due == date(2026, 9, 18)


def test_the_boards_prefill_applies_when_the_line_names_no_date():
    title, due = quickadd.resolve("позвонить Пете", TODAY, chosen=TODAY, picked=False)
    assert title == "позвонить Пете"
    assert due == TODAY


def test_no_date_anywhere_leaves_the_card_undated():
    title, due = quickadd.resolve("позвонить Пете", TODAY)
    assert (title, due) == ("позвонить Пете", None)


def test_clearing_the_date_by_hand_beats_the_prefill():
    """«Без срока» в меню — это выбор: picked=1 с пустой датой. Дата с доски не возвращается."""
    _, due = quickadd.resolve("позвонить Пете", TODAY, chosen=None, picked=True)
    assert due is None


def test_clearing_the_date_by_hand_beats_a_typed_phrase_too():
    """«Без срока» — такой же выбор, как дата: иначе снять срок со строки, где написано
    «до пятницы», было бы нечем, а кнопка показывала бы «срок» при живой дате."""
    _, due = quickadd.resolve(
        "позвонить Пете до пятницы", TODAY, chosen=None, picked=True
    )
    assert due is None
