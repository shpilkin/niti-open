"""Повторный разбор уже разложенного письма: текст или ссылки цепочки пришли позже.

Письмо остаётся на месте, пока ссылки не ведут в другую цепочку. Цепочки при этом не
сливаются: слияние удаляет карточки слитой цепочки, а у лежащих они уже в работе.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from email_reader.ingest import store_email
from email_reader.models import Account, ActionItem, Thread
from email_reader.normalization import EmailAddress, ParsedEmail
from email_reader.pipeline.threading import assign_thread, rethread
from tests.conftest import SPACE_ID, mk_space

DAY0 = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def _parsed(msgid, subject, *, day, in_reply_to=None, refs=()) -> ParsedEmail:
    return ParsedEmail(**{
        "rfc_message_id": msgid,
        "from": EmailAddress(email="bob@x.com"),
        "to": [EmailAddress(email="me@y.ru")],
        "subject": subject,
        "body_text": "b",
        "sent_at": DAY0 + timedelta(days=day),
        "in_reply_to": in_reply_to,
        "references": list(refs),
    })


async def _mailbox(s) -> Account:
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name="Работа", provider="imap", host="h", port=993,
                  login="me@y.ru", password_enc="x", folders=["INBOX"], enabled=True)
    s.add(acc)
    await s.flush()
    return acc


async def _letter(s, acc, parsed, uid):
    email, _ = await store_email(s, account_id=acc.id, parsed=parsed, identities={"me@y.ru"},
                                 folder="INBOX", uid=uid)
    await assign_thread(s, email)
    return email


def _card(email, title):
    return ActionItem(space_id=SPACE_ID, type="MY_COMMITMENT", title=title, status="open",
                      counterparty="Боб", source_thread_id=email.thread_id,
                      source_message_id=email.id)


async def test_a_letter_read_again_does_not_jump_to_a_newer_thread_by_subject(db):
    """Без ссылок новое письмо ищет цепочку по теме, и первой идёт самая свежая. Письмо,
    которое уже лежит в своей цепочке, так уходило бы в чужую."""
    async with db() as s:
        acc = await _mailbox(s)
        first = await _letter(s, acc, _parsed(None, "Смета", day=0), 1)
        again = await _letter(s, acc, _parsed(None, "Смета", day=10), 2)
        later = await _letter(s, acc, _parsed(None, "Смета", day=45), 3)
        assert again.thread_id == first.thread_id != later.thread_id

        await assign_thread(s, again)  # текст пришёл позже — разбор заново

        assert again.thread_id == first.thread_id


async def test_links_that_arrive_later_move_the_letter_and_its_cards(db):
    async with db() as s:
        acc = await _mailbox(s)
        parent = await _letter(s, acc, _parsed("<p@x>", "Смета", day=0), 1)
        letter = await _letter(s, acc, _parsed(None, "Расходы", day=1), 2)
        old = letter.thread_id
        card = _card(letter, "Прислать расходы")
        s.add(card)
        await s.flush()

        letter.in_reply_to = "<p@x>"
        assert await rethread(s, letter) is True

        target = await s.get(Thread, parent.thread_id)
        left = await s.get(Thread, old)
    assert letter.thread_id == parent.thread_id
    assert card.source_thread_id == parent.thread_id
    assert (target.message_count, left.message_count) == (2, 0)
    assert target.extract_version > target.extracted_version  # разобрать цепочку заново


async def test_threads_are_not_merged_when_a_letter_is_read_again(db):
    """Слияние цепочек удаляет карточки той, что сливается. Для нового письма это старое
    правило разбора; для лежащего письма оно стёрло бы карточки, которые уже в работе."""
    async with db() as s:
        acc = await _mailbox(s)
        estimate = await _letter(s, acc, _parsed("<p1@x>", "Смета", day=0), 1)
        contract = await _letter(s, acc, _parsed("<p2@x>", "Договор", day=0), 2)
        reply = await _letter(s, acc, _parsed("<r@x>", "Смета", day=1, in_reply_to="<p1@x>"), 3)
        s.add_all([_card(estimate, "Смета"), _card(contract, "Договор")])
        await s.flush()

        reply.reference_ids = ["<p1@x>", "<p2@x>"]
        await assign_thread(s, reply)
        threads = (await s.execute(select(Thread))).scalars().all()
        cards = (await s.execute(select(ActionItem))).scalars().all()

    assert reply.thread_id == estimate.thread_id
    assert len(threads) == 2 and len(cards) == 2
