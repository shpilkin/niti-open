"""Залежавшиеся карточки: ассистент предлагает закрыть, решает пользователь.

Два сигнала в один флаг (docs/ux-tasks.md §17.4): автомат видит, что собеседник отозвался
после моего обещания, и свёртка планировщика видит, что по карточке давно ничего не
происходит. Второго счётчика не заводим: `next_check` остаётся напоминанием, старение
считается по `updated_at`, а «Оставить» трогает строку — и отсчёт начинается заново.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from email_reader import app_prefs, history, views
from email_reader.commitment import scheduler
from email_reader.enums import AGING_TYPES, ActionStatus, ActionType, CloseHint
from email_reader.models import ActionItem
from email_reader.notify import process_followup_due
from tests.conftest import SPACE_ID, mk_space


def _card(type_, *, days_idle: int, status=ActionStatus.OPEN, candidate=False):
    stamp = datetime.now(UTC) - timedelta(days=days_idle)
    return ActionItem(
        space_id=SPACE_ID, type=str(type_), title="t", status=status,
        counterparty="Таня", updated_at=stamp, created_at=stamp,
        close_candidate=candidate,
    )


async def test_a_promise_nobody_touched_for_a_month_is_offered_for_closing(db):
    async with db() as s:
        await mk_space(s)
        stale = _card(ActionType.MY_COMMITMENT, days_idle=40)
        fresh = _card(ActionType.MY_COMMITMENT, days_idle=3)
        s.add_all([stale, fresh])
        await s.flush()
        assert await scheduler.flag_idle(s, 30) == 1
        await s.refresh(stale)
        await s.refresh(fresh)
        assert stale.close_candidate is True
        assert stale.close_hint == CloseHint.IDLE
        assert fresh.close_candidate is False


async def test_need_my_reply_is_never_aged_but_a_silent_wait_is(db):
    """«Нужен ответ» — мой мяч, пока я не ответил: старить его значило бы предлагать
    закрыть долг. «Жду ответа» с 19.09 затухает вместе с остальным чужим ходом
    (docs/feature-topics.md §8.2); до этого тест утверждал обратное."""
    async with db() as s:
        await mk_space(s)
        mine = _card(ActionType.NEED_MY_REPLY, days_idle=90)
        wait = _card(ActionType.WAITING_FOR_REPLY, days_idle=90)
        s.add_all([mine, wait])
        await s.flush()
        assert await scheduler.flag_idle(s, 30) == 1
        await s.refresh(mine)
        await s.refresh(wait)
        assert mine.close_candidate is False
        assert wait.close_candidate is True and wait.close_hint == CloseHint.IDLE
        assert ActionType.NEED_MY_REPLY not in AGING_TYPES
        assert ActionType.WAITING_FOR_REPLY in AGING_TYPES


async def test_a_letter_in_the_conversation_is_movement(db):
    """Подсказка в настройках обещала «новое письмо в беседе» всегда, а код считал
    только изменение карточки: письмо, не изменившее её при переразборе, не спасало
    от «затухло». Теперь письмо в беседе-источнике — движение в любую сторону."""
    import uuid as _uuid

    from email_reader.enums import Direction
    from email_reader.models import Account, Email, Thread

    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@b.ru", folders=[])
        s.add(acc)
        await s.flush()
        quiet_th, live_th = Thread(account_id=acc.id), Thread(account_id=acc.id)
        s.add_all([quiet_th, live_th])
        await s.flush()
        for th, days in ((quiet_th, 45), (live_th, 3)):
            s.add(Email(account_id=acc.id, thread_id=th.id, dedup_hash=_uuid.uuid4().hex,
                        direction=Direction.INBOUND, from_addr="x@y.z", to_addrs=[],
                        cc_addrs=[], sent_at=datetime.now(UTC) - timedelta(days=days)))
        stale = _card(ActionType.MY_COMMITMENT, days_idle=40)
        stale.source_thread_id = quiet_th.id
        alive = _card(ActionType.MY_COMMITMENT, days_idle=40)
        alive.source_thread_id = live_th.id
        s.add_all([stale, alive])
        await s.flush()
        assert await scheduler.flag_idle(s, 30) == 1
        await s.refresh(stale)
        await s.refresh(alive)
        assert stale.close_candidate is True
        assert alive.close_candidate is False


async def test_a_closed_card_is_not_offered_for_closing_again(db):
    async with db() as s:
        await mk_space(s)
        s.add(_card(ActionType.TASK, days_idle=90, status=ActionStatus.DONE))
        await s.flush()
        assert await scheduler.flag_idle(s, 30) == 0


async def test_zero_days_means_never_offer(db):
    async with db() as s:
        await mk_space(s)
        s.add(_card(ActionType.DELEGATED_TASK, days_idle=900))
        await s.flush()
        assert await scheduler.flag_idle(s, 0) == 0


async def test_keeping_it_open_clears_the_offer_and_restarts_the_clock(db):
    async with db() as s:
        await mk_space(s)
        card = _card(ActionType.DELEGATED_TASK, days_idle=40)
        s.add(card)
        await s.flush()
        await scheduler.flag_idle(s, 30)
        await s.refresh(card)
        assert card.close_candidate is True

        await views.keep_open(s, card.id)
        await s.flush()
        assert card.close_candidate is False
        assert card.close_hint is None
        # Отсчёт начался заново: строку тронули, поэтому свёртка её больше не берёт.
        assert await scheduler.flag_idle(s, 30) == 0
        assert "Оставлено открытым, я решил не закрывать" in [
            e.text for e in await history.for_item(s, card.id)
        ]


async def test_the_reminder_that_used_to_vanish_now_marks_the_card(db):
    """Напоминания уходили в лог, потому что Telegram не настроен. Теперь
    напоминание оставляет след в интерфейсе, даже когда отправлять некуда."""
    async with db() as s:
        await mk_space(s)
        card = _card(ActionType.DELEGATED_TASK, days_idle=1)
        s.add(card)
        await s.flush()
        await process_followup_due(s, {"action_item_id": str(card.id)})
        await s.flush()
        assert card.close_candidate is True
        assert card.close_hint == CloseHint.IDLE
        texts = [e.text for e in await history.for_item(s, card.id)]
        assert "Предложено закрыть: давно ничего не происходит" in texts


async def test_the_offer_lands_in_its_own_section_at_the_bottom(db):
    async with db() as s:
        await mk_space(s)
        plain = _card(ActionType.MY_COMMITMENT, days_idle=1)
        plain.title = "живое обещание"
        offered = _card(ActionType.MY_COMMITMENT, days_idle=40)
        offered.title = "залежалось"
        offered.counterparty = "Пётр"
        s.add_all([plain, offered])
        await s.flush()
        await scheduler.flag_idle(s, 30)
        await s.flush()
        board = await views.board(s, "commitments", SPACE_ID)
        titles = [r.item.title for r in board.rows]
        assert titles == ["живое обещание", "залежалось"]  # предложение — последним
        assert board.sections[-1] == ("Предлагаю закрыть", 1)


async def test_the_scheduler_tick_uses_the_configured_threshold(db):
    async with db() as s:
        await mk_space(s)
        s.add(_card(ActionType.TASK, days_idle=10))
        await s.flush()
        await app_prefs.set_idle_days(s, 7)
        await scheduler.tick(s)
        await s.flush()
        rows = (await s.execute(select(ActionItem))).scalars().all()
        assert [r.close_hint for r in rows] == [str(CloseHint.IDLE)]
