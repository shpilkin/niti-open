"""Совет считает воркер, а не открытая карточка (docs/feature-card-advice.md).

Вызов модели занимает секунды, а в очереди к ней — минуты. Пока его ждала панель,
интерфейс стоял. Теперь карточка живёт сама, а поле «Что делать» заполняется событием;
панель только показывает посчитанное и говорит, почему текста ещё нет.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import email_reader.api.app  # noqa: F401  — регистрирует фильтры шаблонов
from email_reader import advice, model_connections
from email_reader.ai.extractor import Usage
from email_reader.ai.schemas import AdviceResult, Claim, Recommendation
from email_reader.api.common import templates
from email_reader.enums import ActionStatus, ActionType, Direction, EventStatus, EventType
from email_reader.model_connections import ResolvedModel
from email_reader.models import ActionItem, Event, Identity
from tests.conftest import SPACE_ID
from tests.test_memory_context import ME, THEM, _account, _mail, _thread

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
D = timedelta(days=1)
RM = ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")


def _answer():
    return (
        AdviceResult(
            importance=Claim(text="Он ждёт смету", refs=[1]),
            recommendation=Recommendation(kind="reply", why="прямой вопрос", refs=[1]),
            points=[Claim(text="назвать сроки", refs=[1])],
        ),
        Usage(10, 5),
    )


async def _card(s, *, status=ActionStatus.OPEN, title="Ответить про смету"):
    acc = await _account(s)
    s.add(Identity(email=ME))
    th = await _thread(s, acc)
    s.add(_mail(acc, th, direction=Direction.INBOUND, from_addr=THEM, from_name="Пётр Петров",
                to=[(ME, None)], at=NOW - D, subject="Смета"))
    item = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title=title,
                      counterparty="Пётр Петров", status=status, source_thread_id=th.id)
    s.add(item)
    await s.commit()
    return item


async def _events(s) -> list[Event]:
    return list((await s.execute(
        select(Event).where(Event.type == str(EventType.ADVICE_NEEDED))
    )).scalars().all())


def _model(monkeypatch, call):
    monkeypatch.setattr(advice, "structured_call", call)

    async def fake_resolve(*a, **kw):
        return RM

    monkeypatch.setattr(model_connections, "resolve_for_thread", fake_resolve)


async def test_a_card_is_queued_once_until_the_worker_takes_it(db):
    async with db() as s:
        item = await _card(s)

        assert await advice.request(s, item) is True
        await s.commit()
        # Второй заказ на ту же карточку не нужен: одна карточка не должна занимать
        # очередь дважды, пока первое событие не отработало.
        assert await advice.request(s, item) is False
        await s.commit()

        events = await _events(s)
        assert len(events) == 1
        assert events[0].payload == {"action_item_id": str(item.id)}


async def test_a_card_off_the_board_is_not_advised(db):
    """«Готово» и «не обязательство» совет не пересчитывают: их текст замирает."""
    async with db() as s:
        item = await _card(s, status=ActionStatus.DONE)
        assert await advice.request(s, item) is False
        await s.commit()
        assert await _events(s) == []


async def test_a_snoozed_card_still_gets_one(db):
    """Отложенная — не архив: она сама вернётся в «Сегодня», не меняя статуса."""
    async with db() as s:
        item = await _card(s, status=ActionStatus.SNOOZED)
        assert await advice.request(s, item) is True
        await s.commit()
        assert len(await _events(s)) == 1


async def test_the_worker_computes_and_stores_it(db, monkeypatch):
    calls: list[str] = []

    async def fake_call(**kw):
        calls.append(kw["user"])
        return _answer()

    _model(monkeypatch, fake_call)
    async with db() as s:
        item = await _card(s)
        await advice.compute(s, {"action_item_id": str(item.id)})
        await s.commit()

        assert len(calls) == 1
        stored = await advice.latest(s, item.id)
        assert stored is not None and stored.recommendation == "reply"
        assert stored.why == "прямой вопрос" and stored.model == "m"


async def test_a_card_closed_while_it_waited_is_not_computed(db, monkeypatch):
    """Событие стоит в очереди минутами — за это время карточку могли закрыть."""
    async def fail(**kw):
        raise AssertionError("модель не должна вызываться")

    _model(monkeypatch, fail)
    async with db() as s:
        item = await _card(s, status=ActionStatus.DONE)
        await advice.compute(s, {"action_item_id": str(item.id)})
        await s.commit()
        assert await advice.latest(s, item.id) is None


async def test_a_failed_call_reaches_the_queue(db, monkeypatch):
    """Ошибку наружу не глотаем: по ней очередь решает — ждать модель или сдаться."""
    async def boom(**kw):
        raise TimeoutError

    _model(monkeypatch, boom)
    async with db() as s:
        item = await _card(s)
        with pytest.raises(TimeoutError):
            await advice.compute(s, {"action_item_id": str(item.id)})


async def test_the_state_says_why_there_is_no_text_yet(db):
    async with db() as s:
        item = await _card(s)
        assert await advice.state(s, item.id) is None

        ev = Event(type=str(EventType.ADVICE_NEEDED),
                   payload={"action_item_id": str(item.id)}, status=EventStatus.PENDING)
        s.add(ev)
        await s.commit()
        assert await advice.state(s, item.id) == advice.QUEUED

        ev.status = EventStatus.STALLED
        await s.commit()
        assert await advice.state(s, item.id) == advice.UNAVAILABLE

        ev.status = EventStatus.FAILED
        await s.commit()
        assert await advice.state(s, item.id) == advice.FAILED

        ev.status = EventStatus.DONE
        await s.commit()
        assert await advice.state(s, item.id) is None


async def test_the_stored_advice_is_shown_even_when_the_thread_moved_on(db, monkeypatch):
    """Панель показывает последнее посчитанное, а не молчит из-за изменившихся улик:
    пересчёт закажет событие, а до него устаревший текст полезнее пустого поля."""
    _model(monkeypatch, lambda **kw: _done(_answer()))
    async with db() as s:
        item = await _card(s)
        await advice.compute(s, {"action_item_id": str(item.id)})
        await s.commit()

        item.title = "Совсем другая карточка"
        await s.commit()
        prep = await advice.gather(s, item, now=NOW)
        assert await advice.cached(s, item.id, prep.key) is None  # ключ разошёлся
        assert (await advice.latest(s, item.id)).recommendation == "reply"


async def _done(v):
    return v


async def test_the_sweep_takes_only_cards_without_advice(db, monkeypatch):
    """Первое заполнение после обновления: 75 живых карточек не должны разом занять
    очередь, поэтому за заход берём немного — и только тех, у кого совета нет вовсе."""
    _model(monkeypatch, lambda **kw: _done(_answer()))
    async with db() as s:
        first = await _card(s, title="Первая")
        second = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="Вторая",
                            counterparty="Анна Сидорова", status=ActionStatus.OPEN,
                            source_thread_id=first.source_thread_id)
        s.add(second)
        await s.commit()

        await advice.compute(s, {"action_item_id": str(first.id)})
        await s.commit()

        assert await advice.sweep(s, limit=10) == 1
        await s.commit()
        events = await _events(s)
        assert [e.payload["action_item_id"] for e in events] == [str(second.id)]

        # Второй заход не дублирует: событие второй карточки ещё не отработало.
        assert await advice.sweep(s, limit=10) == 0


def _render(**kw):
    item = ActionItem(id=uuid.uuid4(), space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY,
                      title="Ответить про смету", status=ActionStatus.OPEN)
    ctx = {"item": item, "advice": None, "view": "today", "state": None, "no_model": False,
           "importance": "medium", "importance_label": "средняя", **kw}
    return templates.env.get_template("_advice.html").render(**ctx)


def test_the_pane_explains_the_wait_and_asks_again_while_it_lasts():
    queued = _render(state=advice.QUEUED)
    assert "Считаю" in queued
    assert 'hx-trigger="every 8s"' in queued          # поле заполнится само

    stalled = _render(state=advice.UNAVAILABLE)
    assert "Модель недоступна" in stalled and 'hx-trigger="every 8s"' in stalled

    failed = _render(state=advice.FAILED)
    assert "не получился" in failed and "every 8s" not in failed  # ждать нечего

    nothing = _render()
    assert "ещё не посчитан" in nothing and "every 8s" not in nothing

    no_model = _render(no_model=True)
    assert "Модель для пространства не настроена" in no_model


async def test_changing_a_card_by_hand_queues_a_new_advice(db):
    """Перенесли срок — довод про важность («срок 16.09 просрочен») устарел; пересчёт
    заказывает тот же путь, что рисует список, и ждать его никто не будет."""
    from datetime import date

    from email_reader import views
    from email_reader.api.common import _advice_after

    async with db() as s:
        item = await _card(s)
        await views.set_due(s, item.id, date(2026, 9, 20))
        await _advice_after(s, item.id)
        await s.commit()
        assert len(await _events(s)) == 1


async def test_a_letter_in_the_thread_queues_the_advice_with_the_card(db, monkeypatch):
    """Письмо меняет и то, что карточка значит, и что с ней делать. Событие ставится
    рядом с карточкой — но карточка появляется независимо от того, ответит ли модель."""
    from email_reader.commitment import engine
    from tests.conftest import mk_space
    from tests.test_thread_collapse import _it, _seed_thread, _stub_model

    async with db() as s:
        await mk_space(s)
        _, th = await _seed_thread(s, [("inbound", "tanya@x.ru", "me@ya.ru")])
        await s.commit()
        _stub_model(monkeypatch, [_it(ActionType.NEED_MY_REPLY, "tanya@x.ru", "ответить Тане")])

        await engine.process_thread_ready(s, {"thread_id": str(th.id)})
        await s.commit()

        card = (await s.execute(
            select(ActionItem).where(ActionItem.source_thread_id == th.id)
        )).scalar_one()
        events = await _events(s)
        assert [e.payload["action_item_id"] for e in events] == [str(card.id)]
