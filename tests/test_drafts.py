"""Draft recommendations: pure helpers (rendering, follow-up stats, prompt assembly)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from email_reader import drafts
from email_reader.enums import ActionType
from email_reader.models import ActionItem, Email

NOW = datetime(2026, 7, 4, 9, 0, tzinfo=UTC)


def _email(direction: str, body: str, days_ago: int, who: str = "x@y.z") -> Email:
    return Email(
        direction=direction, from_addr=who, from_name=None,
        to_addrs=[], cc_addrs=[], dedup_hash="h",
        sent_at=NOW - timedelta(days=days_ago), body_text=body,
    )


def test_render_tail_keeps_newest_and_order():
    # 6 messages ~1400 chars each: the 6000-char budget forces dropping the oldest.
    emails = [
        _email("inbound", f"START{i} " + "x" * 1400, days_ago=6 - i) for i in range(6)
    ]
    tail = drafts._render_tail(emails)
    assert "START5" in tail  # newest kept
    assert "START0" not in tail  # oldest dropped by the budget
    assert tail.index("START3") < tail.index("START5")  # chronological order restored


def test_followup_stats_counts_trailing_pings():
    emails = [
        _email("inbound", "вопрос", 10),
        _email("outbound", "ответ", 7),
        _email("outbound", "пинг", 3),
    ]
    days, pings = drafts.followup_stats(emails, NOW)
    assert days == 3
    assert pings == 2  # two of my messages in a row without an answer


def _item(t: ActionType) -> ActionItem:
    it = ActionItem(type=t, title="Смета для Кирилла", counterparty="Кирилл")
    it.id = uuid.uuid4()
    return it


def test_user_prompt_blocks_by_type():
    ctx = drafts.DraftContext(
        item=_item(ActionType.WAITING_FOR_REPLY), thread_block="ПЕРЕПИСКА",
        days_silent=5, prior_pings=1,
    )
    p = drafts._user_prompt(ctx)
    assert "пинг №2" in p and "5 дн" in p and "ПЕРЕПИСКА" in p

    ctx2 = drafts.DraftContext(
        item=_item(ActionType.TO_DELEGATE), thread_block="T",
        profile="Роль: PM. Команды: Аналитика.",
    )
    ctx2.item.delegate_to = "Аналитика"
    p2 = drafts._user_prompt(ctx2)
    assert "Кому делегировать: Аналитика" in p2 and "Профиль владельца" in p2

    ctx3 = drafts.DraftContext(item=_item(ActionType.NEED_MY_REPLY), thread_block="T")
    p3 = drafts._user_prompt(ctx3)
    assert "пинг" not in p3 and "делегировать" not in p3.lower()


def test_draftable_covers_exactly_three_types():
    assert set(drafts.DRAFTABLE) == {
        ActionType.NEED_MY_REPLY, ActionType.WAITING_FOR_REPLY, ActionType.TO_DELEGATE
    }


def test_user_prompt_carries_the_plan_before_the_thread():
    ctx = drafts.DraftContext(
        item=_item(ActionType.NEED_MY_REPLY), thread_block="ПЕРЕПИСКА",
        plan=["сроки по API", "кто ведёт интеграцию"],
    )
    p = drafts._user_prompt(ctx)
    assert ("План ответа (совет ассистента по уликам — покрой каждый пункт):\n"
            "- сроки по API\n- кто ведёт интеграцию") in p
    assert p.index("План ответа") < p.index("Переписка:")
    assert "План ответа" not in drafts._user_prompt(
        drafts.DraftContext(item=_item(ActionType.NEED_MY_REPLY), thread_block="T")
    )
