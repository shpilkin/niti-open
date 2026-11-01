"""История карточки: журнал решений, прочитанный по-человечески (docs/ux-tasks.md §17).

Главное, что здесь проверяется: «снята с доски» и «закрыта» — разные события. Большинство
автоматических закрытий — это «модель перестала возвращать пункт»,
и выдавать их за исполненное обязательство история не должна.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from email_reader import history, views
from email_reader.ai.schemas import ExtractedItem
from email_reader.commitment import engine
from email_reader.enums import ActionStatus, ActionType
from email_reader.models import ActionItem, AgentDecision
from tests.conftest import SPACE_ID, mk_space
from tests.test_thread_collapse import _seed_thread, _stub_model

# --- рендер: чистые функции -------------------------------------------------------


def test_supersede_is_removal_not_completion():
    text, actor = history.describe(
        "auto_resolve", {"reason": "superseded after thread update"}
    )
    assert text.startswith("Снята с доски")
    assert "Закрыта" not in text
    assert actor == "drop"


def test_a_real_lifecycle_close_reads_as_a_close():
    text, actor = history.describe("auto_resolve", {"reason": "counterparty replied"})
    assert text == "Закрыта: собеседник ответил"
    assert actor == "auto"


def test_an_unknown_close_reason_is_shown_not_swallowed():
    text, actor = history.describe("auto_resolve", {"reason": "нечто новое"})
    assert "нечто новое" in text
    # Незнакомую причину считаем снятием: обещать исполнение, не зная причины, нельзя.
    assert actor == "drop"


def test_transition_names_both_types_in_russian():
    text, actor = history.describe(
        TRANSITION := history.TRANSITION,
        {"from": "WAITING_FOR_REPLY", "to": "NEED_MY_REPLY", "reason": "пришёл ответ"},
    )
    assert TRANSITION == "transition"
    assert text == "Жду ответа → Нужен ответ: пришёл ответ"
    assert actor == "auto"


def test_my_own_actions_are_marked_as_mine():
    assert history.describe("user_action", {"action": "complete"}) == (
        "Готово, вручную", "user",
    )
    assert history.describe("user_action", {"action": "dismiss"}) == (
        "Не обязательство", "user",
    )
    assert history.describe("user_action", {"action": "retype", "to": "TASK"}) == (
        "Тип изменён вручную: Задача", "user",
    )


def test_dates_from_the_journal_are_shown_the_way_the_card_shows_them():
    text, _ = history.describe("user_action", {"action": "snooze", "until": "2026-09-20"})
    assert text == "Отложено до 20.09.2026"
    assert history.describe("user_action", {"action": "set_due", "due": None})[0] == "Срок снят"


def test_a_thread_level_decision_is_not_card_history():
    assert history.describe("extract", {"item_count": 3}) is None
    assert history.describe("project_match", {}) is None


# --- журнал пишется там, где карточка меняется -------------------------------------


async def _history_texts(s, item_id):
    return [e.text for e in await history.for_item(s, item_id)]


async def test_the_assistant_records_the_type_change_it_makes(db, monkeypatch):
    """Смена типа на месте больше не молчит: в истории видно, из чего во что и почему."""
    _stub_model(monkeypatch, [
        ExtractedItem(
            type=ActionType.NEED_MY_REPLY, title="ответить", counterparty="tanya@x.ru",
            reason="пришёл ответ и новый вопрос",
        )
    ])
    async with db() as s:
        await mk_space(s)
        _, th = await _seed_thread(s, [
            ("outbound", "me@ya.ru", "tanya@x.ru"),
            ("inbound", "tanya@x.ru", "me@ya.ru"),
        ])
        s.add(ActionItem(
            space_id=SPACE_ID, type=str(ActionType.WAITING_FOR_REPLY), title="жду",
            counterparty="tanya@x.ru", status=ActionStatus.OPEN, source_thread_id=th.id,
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        ))
        await s.flush()
        await engine.process_thread_ready(s, {"thread_id": str(th.id)})
        await s.flush()

        card = (await s.execute(select(ActionItem).where(
            ActionItem.source_thread_id == th.id
        ))).scalars().one()
        assert card.type == ActionType.NEED_MY_REPLY
        texts = await _history_texts(s, card.id)
        assert "Жду ответа → Нужен ответ: пришёл ответ и новый вопрос" in texts


async def test_no_journal_entry_when_the_type_did_not_change(db, monkeypatch):
    _stub_model(monkeypatch, [
        ExtractedItem(type=ActionType.NEED_MY_REPLY, title="ответить", counterparty="t@x.ru")
    ])
    async with db() as s:
        await mk_space(s)
        _, th = await _seed_thread(s, [("inbound", "t@x.ru", "me@ya.ru")])
        s.add(ActionItem(
            space_id=SPACE_ID, type=str(ActionType.NEED_MY_REPLY), title="ответить",
            counterparty="t@x.ru", status=ActionStatus.OPEN, source_thread_id=th.id,
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        ))
        await s.flush()
        await engine.process_thread_ready(s, {"thread_id": str(th.id)})
        await s.flush()
        rows = (await s.execute(select(AgentDecision).where(
            AgentDecision.kind == history.TRANSITION
        ))).scalars().all()
        assert list(rows) == []


async def test_my_dismissal_is_in_the_history_instead_of_a_silent_gap(db):
    """«Не обязательство» раньше не писалось в журнал вообще — история показала бы
    закрытие без автора."""
    async with db() as s:
        await mk_space(s)
        ai = ActionItem(
            space_id=SPACE_ID, type=str(ActionType.TASK), title="t",
            status=ActionStatus.OPEN,
        )
        s.add(ai)
        await s.flush()
        await views.dismiss(s, ai.id)
        await s.flush()
        assert await _history_texts(s, ai.id) == ["Не обязательство"]


async def test_history_is_oldest_first(db):
    """Читается как рассказ, а не как лог: сверху то, что случилось раньше. Записи с
    одинаковой секундой упорядочены недетерминированно (в журнале нет счётчика), поэтому
    тест задаёт время явно — проверяется порядок, а не разрешение времени в базе."""
    async with db() as s:
        await mk_space(s)
        ai = ActionItem(
            space_id=SPACE_ID, type=str(ActionType.TASK), title="t",
            status=ActionStatus.OPEN,
        )
        s.add(ai)
        await s.flush()
        for day, payload in (
            (3, {"action": "complete"}),
            (1, {"action": "set_due", "due": None}),
            (2, {"action": "snooze", "until": "2026-09-20"}),
        ):
            s.add(AgentDecision(
                kind="user_action", action_item_id=ai.id, payload=payload,
                created_at=datetime(2026, 9, day, tzinfo=UTC),
            ))
        await s.flush()
        assert await _history_texts(s, ai.id) == [
            "Срок снят", "Отложено до 20.09.2026", "Готово, вручную",
        ]
