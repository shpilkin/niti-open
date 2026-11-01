"""Telegram intake (one bot per space): what a message becomes, where it lands, and how a
bot token is stored. The bots themselves are not exercised — no network in tests."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from email_reader import spaces as spaces_svc
from email_reader import views
from email_reader.enums import ActionType, SourceKind
from email_reader.telegram import intake
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space

TODAY = date(2026, 9, 4)  # Friday


def test_own_text_is_a_task_with_the_date_parsed_out():
    d = intake.classify("позвонить Пете до пятницы", forwarded_from=None, today=TODAY)
    assert (d.type, d.title, d.due, d.counterparty) == (
        ActionType.TASK, "позвонить Пете", date(2026, 9, 11), None,
    )
    assert d.reason == "Из Telegram"


def test_multiline_text_keeps_the_rest_as_reason():
    d = intake.classify("Купить билеты\nна конференцию, до 20 сентября\nи отель",
                        forwarded_from=None, today=TODAY)
    assert d.title == "Купить билеты"
    assert d.due is None  # the date lives in the second line, which is context, not the title
    assert d.reason == "Из Telegram. на конференцию, до 20 сентября и отель"


def test_forwarded_message_is_a_reply_candidate_from_its_author():
    d = intake.classify("можешь глянуть смету до среды?", forwarded_from="Дмитрий Орлов",
                        today=TODAY)
    assert d.type == ActionType.NEED_MY_REPLY
    assert d.counterparty == "Дмитрий Орлов"
    assert d.title == "можешь глянуть смету до среды?"  # the author's words, untouched
    assert d.due is None
    assert d.reason.startswith("Переслано из Telegram, автор: Дмитрий Орлов")


def test_empty_message_is_nothing():
    assert intake.classify("   \n  ", forwarded_from=None, today=TODAY) is None


def test_origin_name_across_aiogram_origin_shapes():
    user = SimpleNamespace(first_name="Анна", last_name="Соколова", username="anna")
    msg = SimpleNamespace(forward_origin=SimpleNamespace(sender_user=user))
    assert intake.origin_name(msg) == "Анна Соколова"
    nameless = SimpleNamespace(first_name=None, last_name=None, username="anna")
    msg = SimpleNamespace(forward_origin=SimpleNamespace(sender_user=nameless))
    assert intake.origin_name(msg) == "@anna"
    hidden = SimpleNamespace(sender_user_name="Скрытый Пользователь")
    assert intake.origin_name(SimpleNamespace(forward_origin=hidden)) == "Скрытый Пользователь"
    channel = SimpleNamespace(chat=SimpleNamespace(title="Канал новостей", username=None))
    assert intake.origin_name(SimpleNamespace(forward_origin=channel)) == "Канал новостей"
    assert intake.origin_name(SimpleNamespace(forward_origin=None)) is None


async def test_ingest_lands_in_the_space_inbox_with_a_source_ref(db):
    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        ai = await intake.ingest(
            s, space_id=SPACE_ID, chat_id=42, message_id=7, text="продлить домен завтра",
            forwarded_from=None, today=TODAY,
        )
        await s.commit()
        assert ai.source_kind == SourceKind.TELEGRAM and ai.source_ref == "42:7"
        assert ai.user_verdict is None and ai.space_id == SPACE_ID
        assert ai.due_date == date(2026, 9, 5)
        inbox = await views.list_view(s, "triage", space_id=SPACE_ID)
        assert [i.title for i in inbox] == ["продлить домен"]
        assert await views.list_view(s, "triage", space_id=SPACE_ID_2) == []
        assert await views.list_view(s, "commitments") == []  # not on a board until accepted


async def test_bot_token_is_stored_encrypted_and_resolved(db):
    async with db() as s:
        await mk_space(s)
        b = await spaces_svc.create_bot(s, SPACE_ID, name="Работа", token="123:abc", chat_id=555)
        await s.commit()
        assert b.token_enc != "123:abc"
        assert spaces_svc.resolve_bot_token(b) == "123:abc"
        assert [x.id for x in await spaces_svc.active_bots(s)] == [b.id]
        # Blank token on update keeps the old one; the chat id can be cleared explicitly.
        await spaces_svc.update_bot(s, b.id, token="", chat_id=None)
        await s.commit()
        assert spaces_svc.resolve_bot_token(b) == "123:abc" and b.chat_id is None
        await spaces_svc.update_bot(s, b.id, token="999:zzz", enabled=False)
        await s.commit()
        assert spaces_svc.resolve_bot_token(b) == "999:zzz"
        assert await spaces_svc.active_bots(s) == []  # switched off -> not polled


def test_space_bot_answers_only_its_own_chat():
    """No network: the dispatcher's account map is filled by hand and the helpers that
    every handler goes through are exercised with fake updates."""
    import uuid

    from aiogram.types import Message

    from email_reader.telegram import bot as tg

    acc_id = uuid.uuid4()
    bot = SimpleNamespace(id=777)
    tg.acc_dp.workflow_data["accounts"] = {777: tg.BotSpace(acc_id, "Работа", 555)}
    try:
        # A Message from the linked chat is allowed; from any other chat it is not.
        mine = Message.model_construct(chat=SimpleNamespace(id=555))
        other = Message.model_construct(chat=SimpleNamespace(id=556))
        assert tg._acc_allowed(mine, bot).space_id == acc_id
        assert tg._acc_allowed(other, bot) is None
        # A callback carries the chat through its message.
        cb = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=555)))
        assert tg._acc_allowed(cb, bot).name == "Работа"
        # An unknown bot (token removed meanwhile) is refused outright.
        assert tg._acc_allowed(mine, SimpleNamespace(id=1)) is None
        # No chat id configured yet -> nothing is allowed (only /start answers).
        tg.acc_dp.workflow_data["accounts"] = {777: tg.BotSpace(acc_id, "Работа", None)}
        assert tg._acc_allowed(mine, bot) is None
    finally:
        tg.acc_dp.workflow_data.pop("accounts", None)


def test_bot_reply_uses_human_type_labels():
    from email_reader.telegram import bot as tg

    item = SimpleNamespace(title="Смета", counterparty="Иванов", type="NEED_MY_REPLY",
                           due_date=date(2026, 9, 10))
    assert tg._fmt(item) == "<b>Смета</b>\nИванов · Нужен ответ · до 2026-09-10"
    item = SimpleNamespace(title="Домен", counterparty=None, type="TASK", due_date=None)
    assert tg._fmt(item) == "<b>Домен</b>\nЗадача"
