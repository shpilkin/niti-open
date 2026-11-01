from __future__ import annotations

import uuid

from email_reader.labels import _normalize_pattern, correspondents, matching_label_ids
from email_reader.models import Email, SenderRule


def _email(direction, from_addr="", to=None, cc=None):
    return Email(
        direction=direction,
        from_addr=from_addr,
        to_addrs=to or [],
        cc_addrs=cc or [],
        dedup_hash="x",
        sent_at=None,
    )


def test_normalize_pattern_strips_at_and_lowercases():
    assert _normalize_pattern("domain", "@Acme.RU") == "acme.ru"
    assert _normalize_pattern("address", "  Ivan@Acme.ru ") == "ivan@acme.ru"


def test_correspondents_inbound_is_sender():
    e = _email("inbound", from_addr="Ivan@Acme.ru")
    assert correspondents(e) == {"ivan@acme.ru"}


def test_correspondents_outbound_is_recipients():
    e = _email("outbound", to=[{"email": "a@acme.ru"}], cc=[{"email": "b@x.com"}])
    assert correspondents(e) == {"a@acme.ru", "b@x.com"}


def test_match_address():
    lid = uuid.uuid4()
    rules = [SenderRule(match_type="address", pattern="ivan@acme.ru", label_id=lid)]
    assert matching_label_ids(rules, {"ivan@acme.ru"}) == {lid}
    assert matching_label_ids(rules, {"other@acme.ru"}) == set()


def test_match_domain():
    lid = uuid.uuid4()
    rules = [SenderRule(match_type="domain", pattern="acme.ru", label_id=lid)]
    assert matching_label_ids(rules, {"anyone@acme.ru"}) == {lid}
    assert matching_label_ids(rules, {"x@other.com"}) == set()


def test_match_multiple_rules():
    a, b = uuid.uuid4(), uuid.uuid4()
    rules = [
        SenderRule(match_type="address", pattern="ivan@acme.ru", label_id=a),
        SenderRule(match_type="domain", pattern="acme.ru", label_id=b),
    ]
    assert matching_label_ids(rules, {"ivan@acme.ru"}) == {a, b}


# --- Метка стоит на письме, а не на беседе ---------------------------------------------
# Правило заводят на адрес; на беседе метка красила всю переписку, и чип висел на письмах
# тех, к кому правило не относится. Этот путь раньше тестами не покрывался вовсе.
from datetime import UTC, datetime  # noqa: E402

import pytest  # noqa: E402

from email_reader import labels as labels_svc  # noqa: E402
from email_reader.models import Account, ActionItem, Label, Thread  # noqa: E402
from tests.conftest import SPACE_ID, mk_space  # noqa: E402

NOW = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)


async def _thread_of_three(s):
    """Беседа из трёх входящих: два разных человека и один, на кого заведено правило."""
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name="Я", provider="imap", login="me@ya.ru",
                  host="imap.x", port=993, enabled=True)
    s.add(acc)
    await s.flush()
    thread = Thread(last_at=NOW, account_id=acc.id)
    s.add(thread)
    await s.flush()
    made = []
    for i, sender in enumerate(("anton@romashka.ru", "boris@romashka.ru", "anton@romashka.ru")):
        e = Email(account_id=acc.id, direction="inbound", from_addr=sender, from_name=None,
                  to_addrs=[], cc_addrs=[], subject="Как выдаются доступы", body_text="т",
                  sent_at=NOW, thread_id=thread.id, dedup_hash=f"h{i}")
        s.add(e)
        await s.flush()
        made.append(e)
    label = Label(name="БЛ", color="#5e30eb")
    s.add(label)
    await s.flush()
    s.add(SenderRule(match_type="address", pattern="boris@romashka.ru",
                     action="label", label_id=label.id))
    await s.flush()
    return made, label


@pytest.mark.asyncio
async def test_the_label_lands_only_on_the_letter_from_that_address(db):
    async with db() as s:
        (first, boris, third), label = await _thread_of_three(s)
        for e in (first, boris, third):
            await labels_svc.apply_rules_to_email(s, e)
        marked = await labels_svc.labels_for_emails(s, [first.id, boris.id, third.id])
    assert list(marked) == [boris.id]
    assert [lbl.name for lbl in marked[boris.id]] == ["БЛ"]


@pytest.mark.asyncio
async def test_applying_rules_to_existing_marks_letters_not_conversations(db):
    async with db() as s:
        (first, boris, third), _ = await _thread_of_three(s)
        touched = await labels_svc.apply_rules_to_existing(s)
        marked = await labels_svc.labels_for_emails(s, [first.id, boris.id, third.id])
        by_label = await labels_svc.email_ids_for_label(
            s, (await labels_svc.list_labels(s))[0].id
        )
    assert touched == 1  # одно письмо, а не вся беседа из трёх
    assert list(marked) == [boris.id]
    assert by_label == [boris.id]


# --- Метка видна и на задаче (docs/ux-tasks.md §17.6) ---------------------------------
# Правило про адрес — это «от кого это»; карточка растёт из беседы с тем же человеком, и
# на строке задачи ответ на тот же вопрос должен читаться тем же чипом, что в почте.
def _card(thread_id, title="Ответить Борису"):
    return ActionItem(
        space_id=SPACE_ID, type="NEED_MY_REPLY", title=title, status="open",
        source_thread_id=thread_id,
    )


@pytest.mark.asyncio
async def test_a_card_shows_the_labels_of_its_thread(db):
    async with db() as s:
        (first, boris, _third), label = await _thread_of_three(s)
        await labels_svc.apply_rules_to_existing(s)
        card = _card(first.thread_id)
        s.add(card)
        await s.flush()
        by_item = await labels_svc.labels_for_items(s, [card])
    # Метка стоит на письме Бориса; карточка принадлежит той же беседе — чип у неё есть.
    assert [lbl.name for lbl in by_item[card.id]] == [label.name]


@pytest.mark.asyncio
async def test_a_card_without_a_thread_has_no_labels(db):
    """Встреча, Telegram, ручная задача: беседы нет — и спрашивать не о чем."""
    async with db() as s:
        await _thread_of_three(s)
        await labels_svc.apply_rules_to_existing(s)
        card = _card(None, title="Сделать презу")
        s.add(card)
        await s.flush()
        by_item = await labels_svc.labels_for_items(s, [card])
    assert by_item == {}


@pytest.mark.asyncio
async def test_a_thread_label_is_listed_once_however_many_letters_carry_it(db):
    async with db() as s:
        (first, boris, _third), _ = await _thread_of_three(s)
        await labels_svc.apply_rules_to_existing(s)
        # Второе письмо того же адресата в той же беседе — метка та же.
        extra = Email(
            account_id=boris.account_id, direction="inbound",
            from_addr="boris@romashka.ru", from_name=None, to_addrs=[], cc_addrs=[],
            subject="ещё", body_text="т", sent_at=NOW, thread_id=first.thread_id,
            dedup_hash="h9",
        )
        s.add(extra)
        await s.flush()
        await labels_svc.apply_rules_to_email(s, extra)
        card = _card(first.thread_id)
        s.add(card)
        await s.flush()
        by_item = await labels_svc.labels_for_items(s, [card])
    assert len(by_item[card.id]) == 1
