"""The counterparty column: filled from the thread when the model leaves it empty, one
line at most, shown as a name in the row."""

from __future__ import annotations

from datetime import UTC, datetime

from email_reader.ai.schemas import CP_LIMIT, ExtractedItem, MeetingItem, one_line
from email_reader.api.common import _who_name
from email_reader.commitment import engine
from email_reader.enums import ActionType
from email_reader.models import Account, ActionItem, Email, Thread
from tests.conftest import SPACE_ID, mk_space
from tests.test_thread_collapse import _it, _live, _stub_model

ME = {"me@ya.ru"}


def _email(direction, frm, to, from_name=None, cc=()):
    return Email(
        direction=direction, from_addr=frm, from_name=from_name,
        to_addrs=[{"email": a, "name": n} for a, n in to],
        cc_addrs=[{"email": a} for a in cc],
    )


def test_need_reply_falls_back_to_the_last_inbound_sender():
    emails = [
        _email("inbound", "petya@x.ru", [("me@ya.ru", None)], from_name="Пётр",
               cc=("tanya@x.ru",)),
        _email("outbound", "me@ya.ru", [("petya@x.ru", "Пётр")]),
        _email("inbound", "kolya@x.ru", [("me@ya.ru", None)], cc=("tanya@x.ru",)),
    ]
    assert engine._sole_counterparty(emails, ME) is None  # several parties: no pin
    assert engine._fallback_counterparty(ActionType.NEED_MY_REPLY, emails, ME) == "kolya@x.ru"
    emails.pop()
    assert (
        engine._fallback_counterparty(ActionType.NEED_MY_REPLY, emails, ME)
        == "Пётр (petya@x.ru)"
    )


def test_my_court_types_fall_back_to_the_sole_recipient_of_my_last_message():
    emails = [
        _email("inbound", "petya@x.ru", [("me@ya.ru", None)], cc=("tanya@x.ru",)),
        _email("outbound", "me@ya.ru", [("tanya@x.ru", "Таня")], cc=("petya@x.ru",)),
    ]
    for t in (ActionType.WAITING_FOR_REPLY, ActionType.DELEGATED_TASK,
              ActionType.MY_COMMITMENT):
        assert engine._fallback_counterparty(t, emails, ME) == "Таня (tanya@x.ru)"
    # Two external recipients: no guessing.
    emails[-1].to_addrs = emails[-1].to_addrs + [{"email": "petya@x.ru"}]
    assert engine._fallback_counterparty(ActionType.WAITING_FOR_REPLY, emails, ME) is None
    # Nothing outbound at all; a type with no counterparty by nature.
    assert engine._fallback_counterparty(ActionType.WAITING_FOR_REPLY, emails[:1], ME) is None
    assert engine._fallback_counterparty(ActionType.TO_DELEGATE, emails, ME) is None


def test_own_address_never_becomes_the_counterparty():
    emails = [_email("inbound", "me@ya.ru", [("me@ya.ru", None)])]
    assert engine._fallback_counterparty(ActionType.NEED_MY_REPLY, emails, ME) is None
    emails = [_email("outbound", "me@ya.ru", [("me@ya.ru", None)])]
    assert engine._fallback_counterparty(ActionType.MY_COMMITMENT, emails, ME) is None


def test_counterparty_is_one_line_and_capped():
    essay = "Дмитрий Ковальчук, Мария Белая\nЯвляется ли это MY_COMMITMENT? Нет. " * 40
    it = ExtractedItem(type=ActionType.MY_COMMITMENT, title="t", counterparty=essay,
                       delegate_to="  \n")
    assert it.counterparty == "Дмитрий Ковальчук, Мария Белая"
    assert it.delegate_to is None
    it = ExtractedItem(type=ActionType.TASK, title="t", counterparty="x" * 300)
    assert len(it.counterparty) == CP_LIMIT
    assert one_line("null") is None and one_line("") is None and one_line(None) is None
    mi = MeetingItem(type=ActionType.MY_COMMITMENT, title="t",
                     counterparty="Иванов\nпотому что он просил")
    assert mi.counterparty == "Иванов"


def test_row_shows_the_name_without_the_address_tail():
    assert _who_name("Пётр Иванов (petya@x.ru)") == "Пётр Иванов"
    assert _who_name("petya@x.ru") == "petya@x.ru"
    assert _who_name("Сервис (noreply@service.example)") == "Сервис"
    assert _who_name("Иванов (см. письмо)") == "Иванов (см. письмо)"  # not an address
    assert _who_name(None) == "" and _who_name("  ") == ""


async def _petya_asks_with_tanya_in_copy(s, mailbox="w"):
    """Petya asks me with Tanya in copy: not a 1-on-1 thread, so nothing is pinned."""
    acc = Account(space_id=SPACE_ID, name=mailbox, host="h", login="me@ya.ru", folders=[])
    s.add(acc)
    await s.flush()
    th = Thread(account_id=acc.id, extract_version=1, extracted_version=0)
    s.add(th)
    await s.flush()
    s.add(Email(
        account_id=acc.id, thread_id=th.id, direction="inbound", from_addr="petya@x.ru",
        from_name="Пётр", to_addrs=[{"email": "me@ya.ru"}],
        cc_addrs=[{"email": "tanya@x.ru"}], dedup_hash="h0",
        sent_at=datetime(2026, 7, 1, tzinfo=UTC), subject="s", body_text="b",
    ))
    await s.flush()
    return th


async def test_extraction_fills_the_empty_field_from_the_thread(db, monkeypatch):
    async with db() as s:
        await mk_space(s)
        th = await _petya_asks_with_tanya_in_copy(s)
        await s.commit()
        _stub_model(monkeypatch, [_it(ActionType.NEED_MY_REPLY, None, "ответить Пете")])
        await engine.process_thread_ready(s, {"thread_id": str(th.id)})
        await s.commit()
        live = await _live(s, th.id)
        assert [(i.type, i.counterparty) for i in live] == [
            (ActionType.NEED_MY_REPLY, "Пётр (petya@x.ru)")
        ]


async def test_a_card_saved_without_a_counterparty_is_named_in_place(db, monkeypatch):
    """The cards already on the boards with an empty column: on the thread's next mail
    the same card (same id) gets the name — not a twin, not a superseded-and-recreated
    copy that would lose its snooze and its age."""
    async with db() as s:
        await mk_space(s)
        th = await _petya_asks_with_tanya_in_copy(s)
        old = ActionItem(space_id=SPACE_ID, type=str(ActionType.NEED_MY_REPLY),
                         title="ответить Пете", counterparty=None, status="open",
                         source_thread_id=th.id)
        s.add(old)
        await s.commit()
        old_id = old.id
        _stub_model(monkeypatch, [_it(ActionType.NEED_MY_REPLY, None, "ответить Пете")])
        await engine.process_thread_ready(s, {"thread_id": str(th.id)})
        await s.commit()
        live = await _live(s, th.id)
        assert [(i.id, i.counterparty) for i in live] == [(old_id, "Пётр (petya@x.ru)")]


async def test_a_user_edited_card_without_a_counterparty_gets_no_twin(db, monkeypatch):
    async with db() as s:
        await mk_space(s)
        th = await _petya_asks_with_tanya_in_copy(s)
        mine = ActionItem(space_id=SPACE_ID, type=str(ActionType.NEED_MY_REPLY),
                          title="моя формулировка", counterparty=None, status="open",
                          user_edited=True, source_thread_id=th.id)
        s.add(mine)
        await s.commit()
        mine_id = mine.id
        _stub_model(monkeypatch, [_it(ActionType.NEED_MY_REPLY, None, "ответить Пете")])
        await engine.process_thread_ready(s, {"thread_id": str(th.id)})
        await s.commit()
        live = await _live(s, th.id)
        assert [(i.id, i.title, i.counterparty) for i in live] == [
            (mine_id, "моя формулировка", None)
        ]


async def test_backfill_names_the_old_cards_once_and_leaves_the_rest(db):
    """Cards saved before the fallback existed: the startup pass names them in place.
    Untouched: user-edited cards, types with no counterparty, a card whose name would
    collide with an existing card of the same thread and type. Runs once."""
    async with db() as s:
        await mk_space(s)
        th = await _petya_asks_with_tanya_in_copy(s)
        old = ActionItem(space_id=SPACE_ID, type=str(ActionType.NEED_MY_REPLY),
                         title="ответить", counterparty=None, status="open",
                         source_thread_id=th.id)
        mine = ActionItem(space_id=SPACE_ID, type=str(ActionType.NEED_MY_REPLY),
                          title="моя", counterparty=None, status="open", user_edited=True,
                          source_thread_id=th.id)
        todo = ActionItem(space_id=SPACE_ID, type=str(ActionType.TO_DELEGATE),
                          title="передать", counterparty=None, status="open",
                          source_thread_id=th.id)
        promised = ActionItem(space_id=SPACE_ID, type=str(ActionType.MY_COMMITMENT),
                              title="обещал", counterparty=None, status="open",
                              source_thread_id=th.id)  # nothing outbound: stays empty
        th2 = await _petya_asks_with_tanya_in_copy(s, mailbox="w2")
        named = ActionItem(space_id=SPACE_ID, type=str(ActionType.NEED_MY_REPLY),
                           title="ответить", counterparty="Пётр (petya@x.ru)",
                           status="open", source_thread_id=th2.id)
        twin = ActionItem(space_id=SPACE_ID, type=str(ActionType.NEED_MY_REPLY),
                          title="ответить ещё", counterparty=None, status="open",
                          source_thread_id=th2.id)
        s.add_all([old, mine, todo, promised, named, twin])
        await s.commit()
        # Scanned: old, promised, twin. Not even looked at: the user-edited card, the
        # TO_DELEGATE card, the card that already has a name.
        assert await engine.backfill_counterparties(s) == {"scanned": 3, "named": 1}
        await s.commit()
        assert old.counterparty == "Пётр (petya@x.ru)"
        assert mine.counterparty is None and todo.counterparty is None
        assert promised.counterparty is None and twin.counterparty is None
        assert await engine.backfill_counterparties(s) == {"scanned": 0, "named": 0}
