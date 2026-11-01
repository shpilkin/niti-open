"""Regression tests for the commitment/mail features built across recent iterations:
read/unread, snooze wake + un-snooze on retype/edit, retype merge, create-from-email,
source-mailbox filter, full-text search, per-account sent, and local-copy-on-send.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from email_reader import views
from email_reader.enums import ActionStatus, ActionType
from email_reader.models import Account, ActionItem, Email, EmailOccurrence, Thread
from tests.conftest import SPACE_ID, mk_space

NOW = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


async def _account(s, name="Acc", login="me@y.ru"):
    a = Account(space_id=SPACE_ID, name=name, provider="yandex", host="h", port=993, login=login,
                password_enc="x", folders=["INBOX"], enabled=True)
    s.add(a)
    await s.flush()
    return a


async def _thread(s, last_at=NOW):
    t = Thread(last_at=last_at)
    s.add(t)
    await s.flush()
    return t


_uid = iter(range(1, 100000))


async def _email(s, thread, *, direction="inbound", from_addr="bob@x.com", from_name=None,
                 subject="S", body="B", html=None, account=None, sent_at=NOW):
    e = Email(direction=direction, from_addr=from_addr, from_name=from_name, to_addrs=[],
              cc_addrs=[], subject=subject, body_text=body, body_raw_html=html,
              sent_at=sent_at, thread_id=thread.id, dedup_hash=str(uuid.uuid4()),
              account_id=account.id if account is not None else None)
    s.add(e)
    await s.flush()
    if account is not None:
        s.add(EmailOccurrence(email_id=e.id, account_id=account.id, folder="INBOX",
                              uid=next(_uid)))
    return e


async def _item(s, thread, *, type_="MY_COMMITMENT", counterparty="bob@x.com",
                status="open", next_check=None):
    ai = ActionItem(space_id=SPACE_ID, type=type_, title="t", counterparty=counterparty,
                    source_thread_id=thread.id, status=status, next_check=next_check)
    s.add(ai)
    await s.flush()
    return ai


# ---- read / unread ----

async def test_read_unread_and_count(db):
    async with db() as s:
        await mk_space(s)
        acc = await _account(s)
        t = await _thread(s)
        e = await _email(s, t, account=acc)
        eid = e.id
        assert await views.unread_inbox_count(s) == 1  # inbound + read_at NULL
        await views.set_read(s, eid, True)
        assert (await views.get_email(s, eid)).read_at is not None
        assert await views.unread_inbox_count(s) == 0
        await views.set_read(s, eid, False)
        assert (await views.get_email(s, eid)).read_at is None
        assert await views.unread_inbox_count(s) == 1


# ---- snooze: hidden from thematic, wakes in "today", listed in "snoozed" ----

async def test_snooze_visibility(db):
    async with db() as s:
        await mk_space(s)
        t = await _thread(s)
        future = await _item(s, t, type_="MY_COMMITMENT", status="snoozed",
                             next_check=datetime.now(UTC).date() + timedelta(days=30))
        fid = future.id
        commitments = [i.id for i in await views.list_view(s, "commitments")]
        snoozed = [i.id for i in await views.list_view(s, "snoozed")]
        today = [i.id for i in await views.list_view(s, "today")]
        assert fid not in commitments  # snoozed hidden from its type view
        assert fid in snoozed
        assert fid not in today  # wakes only on next_check day


async def test_snooze_wakes_in_today(db):
    async with db() as s:
        await mk_space(s)
        t = await _thread(s)
        woken = await _item(s, t, status="snoozed", next_check=date(2026, 1, 1))  # past
        today = [i.id for i in await views.list_view(s, "today")]
        assert woken.id in today


# ---- retype / edit un-snooze + merge ----

async def test_retype_unsnoozes_and_moves(db):
    async with db() as s:
        await mk_space(s)
        t = await _thread(s)
        ai = await _item(s, t, type_="MY_COMMITMENT", status="snoozed",
                         next_check=date(2026, 7, 30))
        await views.set_type(s, ai.id, "NEED_MY_REPLY")
        moved = await views.get_item(s, ai.id)
        assert moved.type == ActionType.NEED_MY_REPLY
        assert moved.status == ActionStatus.OPEN
        assert ai.id in [i.id for i in await views.list_view(s, "needreply")]
        assert ai.id not in [i.id for i in await views.list_view(s, "snoozed")]


async def test_edit_type_unsnoozes(db):
    async with db() as s:
        await mk_space(s)
        t = await _thread(s)
        ai = await _item(s, t, type_="MY_COMMITMENT", status="snoozed",
                         next_check=date(2026, 7, 30))
        await views.edit(s, ai.id, type_="WAITING_FOR_REPLY")
        moved = await views.get_item(s, ai.id)
        assert moved.type == ActionType.WAITING_FOR_REPLY
        assert moved.status == ActionStatus.OPEN


async def test_unread_inbox_is_per_account(db):
    """Sidebar unread badge must be scoped per mailbox (regression: a new mailbox showed
    the same global count as an existing one)."""
    async with db() as s:
        await mk_space(s)
        a1 = await _account(s, name="A1", login="a1@x.ru")
        a2 = await _account(s, name="A2", login="a2@x.ru")
        t = await _thread(s)
        await _email(s, t, direction="inbound", account=a1)
        await _email(s, t, direction="inbound", account=a1)
        by_acc = await views.unread_inbox_by_account(s)
        assert by_acc.get(a1.id) == 2
        assert by_acc.get(a2.id) is None  # empty mailbox -> absent -> no badge


async def test_retype_same_type_unsnoozes(db):
    """Dragging a snoozed item into the section that matches its CURRENT type must still
    revive it (bug: same-type retype returned early and left it snoozed → invisible in
    the type view while stuck in 'Отложенные')."""
    async with db() as s:
        await mk_space(s)
        t = await _thread(s)
        ai = await _item(s, t, type_="NEED_MY_REPLY", status="snoozed",
                         next_check=date(2026, 7, 30))
        await views.set_type(s, ai.id, "NEED_MY_REPLY")  # same type — drag into "Нужен ответ"
        moved = await views.get_item(s, ai.id)
        assert moved.status == ActionStatus.OPEN
        assert moved.next_check is None
        assert ai.id in [i.id for i in await views.list_view(s, "needreply")]
        assert ai.id not in [i.id for i in await views.list_view(s, "snoozed")]


async def test_retype_merge_revives_snoozed_target(db):
    """Merging into an existing sibling revives it even when the sibling was snoozed
    (not only DONE/DISMISSED), so the result shows up in the target section."""
    async with db() as s:
        await mk_space(s)
        t = await _thread(s)
        keep = await _item(s, t, type_="WAITING_FOR_REPLY", counterparty="z@x.com",
                           status="snoozed", next_check=date(2026, 7, 30))
        drag = await _item(s, t, type_="MY_COMMITMENT", counterparty="z@x.com")
        await views.set_type(s, drag.id, "WAITING_FOR_REPLY")  # merges into `keep`
        assert await views.get_item(s, drag.id) is None
        revived = await views.get_item(s, keep.id)
        assert revived.status == ActionStatus.OPEN
        assert keep.id in [i.id for i in await views.list_view(s, "waiting")]


async def test_retype_merges_on_conflict(db):
    async with db() as s:
        await mk_space(s)
        t = await _thread(s)
        keep = await _item(s, t, type_="WAITING_FOR_REPLY", counterparty="z@x.com")
        drag = await _item(s, t, type_="MY_COMMITMENT", counterparty="z@x.com")
        await views.set_type(s, drag.id, "WAITING_FOR_REPLY")  # collides with `keep`
        assert await views.get_item(s, drag.id) is None  # merged away
        assert await views.get_item(s, keep.id) is not None
        from sqlalchemy import func, select
        n = (await s.execute(select(func.count(ActionItem.id)))).scalar_one()
        assert n == 1


# ---- create item from an email ----

async def test_create_item_from_inbound_email(db):
    async with db() as s:
        await mk_space(s)
        t = await _thread(s)
        e = await _email(s, t, direction="inbound", from_addr="boss@x.com", from_name="Boss",
                         account=await _account(s))
        ai = await views.create_item_from_email(s, e.id, "NEED_MY_REPLY")
        assert ai.type == "NEED_MY_REPLY"
        assert ai.counterparty == "Boss"  # from_name of the sender
        assert ai.source_message_id == e.id
        # de-dupe: same (thread, type, counterparty) returns the existing one
        again = await views.create_item_from_email(s, e.id, "NEED_MY_REPLY")
        assert again.id == ai.id


async def test_create_item_from_outbound_email(db):
    async with db() as s:
        await mk_space(s)
        t = await _thread(s)
        e = await _email(s, t, direction="outbound", from_addr="me@y.ru",
                         subject="Re: deal", account=await _account(s))
        e.to_addrs = [{"name": "Client", "email": "client@x.com"}]
        await s.flush()
        ai = await views.create_item_from_email(s, e.id, "MY_COMMITMENT")
        assert ai.counterparty == "Client"  # recipient for outbound


# ---- source-mailbox filter ----

async def test_source_filter_and_map(db):
    async with db() as s:
        await mk_space(s)
        work = await _account(s, "Работа", "work@y.ru")
        home = await _account(s, "Дом", "home@g.com")
        tw = await _thread(s)
        await _email(s, tw, account=work)
        iw = await _item(s, tw, type_="NEED_MY_REPLY")
        th = await _thread(s)
        await _email(s, th, account=home)
        await _item(s, th, type_="NEED_MY_REPLY")

        assert len(await views.list_view(s, "needreply")) == 2
        assert [i.id for i in await views.list_view(s, "needreply", account_id=work.id)] == [iw.id]
        assert len(await views.list_view(s, "needreply", account_id=home.id)) == 1

        amap = await views.accounts_for_threads(s, [tw.id, th.id])
        assert amap[tw.id][1] == "Работа"
        assert amap[th.id][1] == "Дом"


# ---- search: subject, body, sender, and staying in step with the store ----

async def _find(s, text):
    """Unscoped, like the mail these helpers make: it belongs to no mailbox, and a
    scoped search deliberately shows a space only its own mailboxes' letters."""
    from email_reader.search import query as search_query

    _, results = await search_query.search(s, text, space_id=None)
    return results


async def test_search_matches_subject_body_and_sender(db):
    from email_reader.search import index as search_index

    async with db() as s:
        await mk_space(s)
        t = await _thread(s)
        await _email(s, t, from_addr="ivan@x.com", from_name="Иван",
                     subject="Договор аренды", body="пришлите до пятницы")
        await search_index.reconcile(s)
        assert len(await _find(s, "договор")) == 1    # subject
        assert len(await _find(s, "пятницы")) == 1    # body
        assert len(await _find(s, "Иван")) == 1       # sender
        assert len(await _find(s, "вертолёт")) == 0


async def test_a_deleted_letter_stops_being_found(db):
    """Nothing sweeps the index on delete — the hit is dropped when the row behind it
    is gone, and the sweep removes the document later."""
    from sqlalchemy import delete

    from email_reader.search import index as search_index

    async with db() as s:
        await mk_space(s)
        t = await _thread(s)
        e = await _email(s, t, subject="Гелиоген", body="x")
        await search_index.reconcile(s)
        assert len(await _find(s, "гелиоген")) == 1
        await s.execute(delete(Email).where(Email.id == e.id))
        assert len(await _find(s, "гелиоген")) == 0
        assert (await search_index.reconcile(s))["removed"] == 1


# ---- per-account "Отправленные" by sender (local copy visible before sync) ----

async def test_sent_listed_by_sender_without_occurrence(db):
    async with db() as s:
        await mk_space(s)
        acc = await _account(s, "Y", "me@y.ru")
        t = await _thread(s)
        # outbound from the account login, but NO occurrence yet (just-sent local copy)
        await _email(s, t, direction="outbound", from_addr="me@y.ru", subject="Sent it")
        sent = await views.list_mail(s, "sent", account_id=acc.id)
        assert [e.subject for e in sent] == ["Sent it"]


# ---- compose.send stores a local outbound copy + enqueues processing ----

async def test_send_stores_local_outbound_copy(db, monkeypatch):
    from sqlalchemy import func, select

    from email_reader import accounts as accounts_svc
    from email_reader import compose
    from email_reader.mailer import build_message
    from email_reader.models import Event

    async def fake_send_mail(account, password, *, to, subject, body,
                             in_reply_to=None, references=None, attachments=None,
                             html_body=None, cc=""):
        return build_message(account.login, to, subject, body, in_reply_to, references,
                             html_body=html_body, cc=cc)

    monkeypatch.setattr(compose, "send_mail", fake_send_mail)

    async with db() as s:

        await mk_space(s)
        acc = await accounts_svc.create_account(
            s, name="Y", provider="yandex", host="imap.yandex.ru", port=993,
            login="me@y.ru", password="secret", folders=["INBOX", "Sent"],
        )
        result = await compose.send(
            s, account_id=acc.id, to="x@x.com", subject="Hi", body="я обещаю завтра",
            in_reply_to=None, references=[],
        )
        assert result.error is None
        stored = (await s.execute(select(Email).where(Email.subject == "Hi"))).scalars().all()
        assert len(stored) == 1
        assert stored[0].direction == "outbound"  # login is an identity -> outbound
        n_events = (
            await s.execute(select(func.count(Event.id)).where(Event.type == "EmailStored"))
        ).scalar_one()
        assert n_events == 1


async def test_mail_grouping_shows_one_row_per_conversation(db):
    """Группировка по теме, как в классических клиентах: одна строка на переписку —
    последнее письмо и размер переписки. Счёт по тем письмам, что есть в ящике."""
    from datetime import UTC, datetime, timedelta

    from email_reader import views
    from email_reader.models import Account, Email, EmailOccurrence, Thread
    from tests.conftest import SPACE_ID, mk_space

    now = datetime.now(UTC)
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@corp.ru", folders=[])
        s.add(acc)
        await s.flush()
        t = Thread(account_id=acc.id)
        s.add(t)
        await s.flush()
        for i in range(3):
            e = Email(account_id=acc.id, thread_id=t.id, direction="inbound",
                      from_addr="petr@corp.ru", to_addrs=[], cc_addrs=[],
                      subject=f"Смета {i}", body_text="текст",
                      sent_at=now - timedelta(hours=3 - i), dedup_hash=f"h{i}")
            s.add(e)
            await s.flush()
            s.add(EmailOccurrence(email_id=e.id, account_id=acc.id, folder="INBOX", uid=i))
        alone = Email(account_id=acc.id, direction="inbound", from_addr="ivan@corp.ru",
                      to_addrs=[], cc_addrs=[], subject="Отдельное", body_text="текст",
                      sent_at=now, dedup_hash="solo")
        s.add(alone)
        await s.flush()
        s.add(EmailOccurrence(email_id=alone.id, account_id=acc.id, folder="INBOX", uid=9))
        await s.commit()

        plain = await views.list_mail(s, "inbox", account_id=acc.id)
        rows, sizes = await views.list_mail_threads(s, "inbox", account_id=acc.id)
        total = await views.count_mail_threads(s, "inbox", account_id=acc.id)

    assert len(plain) == 4  # без группировки — все письма
    assert [e.subject for e in rows] == ["Отдельное", "Смета 2"]  # последнее в переписке
    assert total == 2
    assert sizes[rows[1].id].count == 3
    # Точка гаснет по всей беседе: здесь непрочитанными считаются все три письма.
    assert sizes[rows[1].id].unread == 3
    # Письмо без треда — само себе переписка, а не общая группа с другими такими же.
    assert sizes[rows[0].id].count == 1


async def test_recipient_suggestions_come_from_the_correspondence(db):
    """Профилей людей в базе единицы, а корреспондентов сотни — подсказка берёт адреса
    из переписки. Ищет и по имени, и по адресу; свои адреса не предлагает."""
    from datetime import UTC, datetime, timedelta

    from email_reader import contacts, identities
    from email_reader.models import Account, Email
    from tests.conftest import SPACE_ID, mk_space

    now = datetime.now(UTC)
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@corp.ru", folders=[])
        s.add(acc)
        await s.flush()
        await identities.add_identity(s, "me@corp.ru")
        for i, (addr, name) in enumerate((
            ("agolubev@romashka.ru", "Голубев Андрей Игоревич"),
            ("tkarimov@romashka.ru", "Каримов Тимур"),
            ("me@corp.ru", "Я сам"),
        )):
            s.add(Email(account_id=acc.id, direction="inbound", from_addr=addr,
                        from_name=name, to_addrs=[], cc_addrs=[], subject=f"письмо {i}",
                        body_text="текст", sent_at=now - timedelta(hours=i),
                        dedup_hash=f"h{i}"))
        await s.commit()

        # Кириллица: lower() в SQLite не-ASCII не трогает, поэтому отбор идёт в Python.
        by_name = await contacts.suggest(s, "голубев", space_id=SPACE_ID)
        by_addr = await contacts.suggest(s, "karim", space_id=SPACE_ID)
        mine = await contacts.suggest(s, "me@corp", space_id=SPACE_ID)
        short = await contacts.suggest(s, "т", space_id=SPACE_ID)

    assert [x.email for x in by_name] == ["agolubev@romashka.ru"]
    assert by_name[0].label == "Голубев Андрей Игоревич <agolubev@romashka.ru>"
    assert [x.email for x in by_addr] == ["tkarimov@romashka.ru"]
    assert mine == []      # свой адрес подсказывать незачем
    assert short == []     # одна буква — это ещё не запрос


async def test_suggestions_do_not_cross_spaces(db):
    """Ящики смешиваются внутри пространства и никогда между ними (docs/spaces.md)."""
    from datetime import UTC, datetime

    from email_reader import contacts
    from email_reader.models import Account, Email
    from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space

    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        other = Account(space_id=SPACE_ID_2, name="дом", host="h", login="home@g.com",
                        folders=[])
        s.add(other)
        await s.flush()
        s.add(Email(account_id=other.id, direction="inbound", from_addr="petr@home.ru",
                    from_name="Пётр", to_addrs=[], cc_addrs=[], subject="привет",
                    body_text="текст", sent_at=datetime.now(UTC), dedup_hash="x"))
        await s.commit()

        assert await contacts.suggest(s, "петр", space_id=SPACE_ID) == []
        assert [x.email for x in await contacts.suggest(s, "петр", space_id=SPACE_ID_2)] == [
            "petr@home.ru"
        ]


async def test_someone_i_only_wrote_to_is_suggested_too(db):
    """Человек может не ответить ни разу — во входящих его нет вовсе, а писать ему
    приходится. Адреса из своих отправленных тоже идут в подсказку."""
    from datetime import UTC, datetime

    from email_reader import contacts
    from email_reader.models import Account, Email
    from tests.conftest import SPACE_ID, mk_space

    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@corp.ru", folders=[])
        s.add(acc)
        await s.flush()
        s.add(Email(account_id=acc.id, direction="outbound", from_addr="me@corp.ru",
                    from_name="Я", to_addrs=[{"name": "Иван Молчун", "email": "ivan@corp.ru"}],
                    cc_addrs=[{"name": None, "email": "cc@corp.ru"}], subject="вопрос",
                    body_text="текст", sent_at=datetime.now(UTC), dedup_hash="s1"))
        await s.commit()

        by_name = await contacts.suggest(s, "молчун", space_id=SPACE_ID)
        in_copy = await contacts.suggest(s, "cc@corp", space_id=SPACE_ID)

    assert [x.email for x in by_name] == ["ivan@corp.ru"]
    assert [x.email for x in in_copy] == ["cc@corp.ru"]


async def test_a_conversation_is_read_when_all_of_its_letters_are(db):
    """Головная строка группы — состояние всей беседы, а не последнего письма: иначе
    точка висит над раскрытой и уже вычитанной группой."""
    from datetime import UTC, datetime, timedelta

    from email_reader import views
    from email_reader.models import Account, Email, EmailOccurrence, Thread
    from tests.conftest import SPACE_ID, mk_space

    now = datetime.now(UTC)
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@corp.ru", folders=[])
        s.add(acc)
        await s.flush()
        t = Thread(account_id=acc.id)
        s.add(t)
        await s.flush()
        letters = []
        for i in range(2):
            e = Email(account_id=acc.id, thread_id=t.id, direction="inbound",
                      from_addr="petr@corp.ru", to_addrs=[], cc_addrs=[],
                      subject=f"Смета {i}", body_text="текст",
                      sent_at=now - timedelta(hours=1 - i), dedup_hash=f"r{i}")
            s.add(e)
            await s.flush()
            s.add(EmailOccurrence(email_id=e.id, account_id=acc.id, folder="INBOX", uid=i))
            letters.append(e)
        await s.commit()

        _, meta = await views.list_mail_threads(s, "inbox", account_id=acc.id)
        head = next(iter(meta.values()))
        assert head.unread == 2

        # Прочитали последнее — беседа всё ещё непрочитана.
        letters[1].read_at = now
        await s.commit()
        _, meta = await views.list_mail_threads(s, "inbox", account_id=acc.id)
        assert next(iter(meta.values())).unread == 1

        letters[0].read_at = now
        await s.commit()
        _, meta = await views.list_mail_threads(s, "inbox", account_id=acc.id)
        assert next(iter(meta.values())).unread == 0


def test_a_plain_text_signature_has_no_html_entities():
    """nh3 снимает теги, но сущности оставляет, и «&nbsp;» уезжал прямо в text/plain
    отправленного письма — получатель видел его буквами."""
    from email_reader.htmlview import signature_to_text

    got = signature_to_text(
        "<div>Глеб Миронов</div>"
        "<div>PM&nbsp;продукта&nbsp;| Ромашка</div>"
        "<div>тел.:&nbsp;+7 (900) 000-00-00 &amp; факс</div>"
    )

    assert "&nbsp;" not in got
    assert "&amp;" not in got
    assert " " not in got  # в текстовой части неразрывный пробел не нужен
    assert "PM продукта | Ромашка" in got
    assert "тел.: +7 (900) 000-00-00 & факс" in got


async def test_the_reply_quotes_the_whole_chain_not_the_short_plain_part(db):
    """text/plain у писем часто несёт только последнее сообщение, а всю переписку —
    text/html. Цитата бралась из короткой части и обрывала цепочку."""
    from datetime import UTC, datetime

    from email_reader import compose
    from email_reader.models import Account, Email
    from tests.conftest import SPACE_ID, mk_space

    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@corp.ru", folders=[])
        s.add(acc)
        await s.flush()
        e = Email(account_id=acc.id, direction="inbound", from_addr="petr@corp.ru",
                  from_name="Пётр", to_addrs=[], cc_addrs=[], subject="Смета",
                  body_text="Последнее сообщение.",
                  body_raw_html=("<div>Последнее сообщение.</div><hr>"
                                 "<div>От: Иван<br>Тема: Смета</div>"
                                 "<div>Первое сообщение цепочки&nbsp;— важное.</div>"),
                  sent_at=datetime.now(UTC), dedup_hash="q1")
        s.add(e)
        await s.flush()
        ctx = await compose.reply_context(s, e.id)

    assert "Первое сообщение цепочки" in ctx["quoted"]
    assert "Последнее сообщение." in ctx["quoted"]
    assert "&nbsp;" not in ctx["quoted"]  # сущности в текстовой цитате не нужны
    assert "<div>" not in ctx["quoted"]   # и разметка тоже


def test_a_letter_keeps_its_own_fonts_but_not_dangerous_css():
    """nh3 по умолчанию срезает инлайновые стили, и письмо приходило чужим шрифтом:
    corporate-почта задаёт calibri/11pt именно так. Оформление оставляем, накрывашки и
    картинки-счётчики через CSS — нет."""
    from email_reader.htmlview import sanitize_email_html

    got = sanitize_email_html(
        '<div style="font-family:calibri,sans-serif;font-size:11pt;color:#1F497D">Текст</div>'
        '<span style="position:fixed;top:0;z-index:99">накрывашка</span>'
        '<div style="background:url(https://tracker.example/x.png)">счётчик</div>'
        '<table style="border-collapse:collapse"><tr><td style="padding:4px">я</td></tr></table>'
    )

    assert "font-family:calibri" in got.replace(" ,", ",")
    assert "font-size:11pt" in got and "#1F497D" in got
    assert "border-collapse:collapse" in got and "padding:4px" in got
    assert "position" not in got and "z-index" not in got
    assert "tracker.example" not in got


