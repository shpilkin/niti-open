"""Spaces (docs/spaces.md): the boundary in the places the boards, mail lists and the
switcher cross it, and the service rules (default space, moving a mailbox, deleting)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from email_reader import accounts as accounts_svc
from email_reader import spaces as spaces_svc
from email_reader import views
from email_reader.api.common import _list_ctx
from email_reader.api.routers.boards import safe_next
from email_reader.enums import ActionType
from email_reader.models import Account, ActionItem, Email, EmailOccurrence, Thread
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space


async def _mailbox(s, space_id, name, login) -> Account:
    a = Account(space_id=space_id, name=name, host="h", login=login, folders=[])
    s.add(a)
    await s.flush()
    return a


async def _mail(s, account, subject) -> Email:
    t = Thread(account_id=account.id)
    s.add(t)
    await s.flush()
    e = Email(account_id=account.id, thread_id=t.id, direction="inbound", from_addr="b@x.ru",
              from_name="Боб", to_addrs=[], cc_addrs=[], subject=subject, body_text="текст",
              sent_at=datetime.now(UTC), dedup_hash=str(uuid.uuid4()))
    s.add(e)
    await s.flush()
    s.add(EmailOccurrence(email_id=e.id, account_id=account.id, folder="INBOX", uid=1))
    await s.flush()
    return e


async def test_a_stale_mailbox_chip_from_another_space_is_ignored(db):
    """The chip cookie survives a switch. A chip naming a mailbox of another space would
    empty every board with no chip bar to clear it — so it is dropped, not applied."""
    async with db() as s:
        sp1 = await mk_space(s)
        await mk_space(s, 2)
        home = await _mailbox(s, SPACE_ID_2, "Дом", "h@x.ru")
        s.add(ActionItem(space_id=SPACE_ID, type=ActionType.TASK, title="mine", status="open"))
        await s.commit()
        ctx = await _list_ctx(s, "commitments", sp1, str(home.id))
        assert [i.title for i in ctx["items"]] == ["mine"]
        assert ctx["active_account"] == "" and ctx["accounts"] == []
        ctx = await _list_ctx(s, "commitments", sp1, "not-a-uuid")
        assert [i.title for i in ctx["items"]] == ["mine"]


async def test_label_listing_and_search_stay_inside_the_space(db):
    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        work = await _mailbox(s, SPACE_ID, "Работа", "w@x.ru")
        home = await _mailbox(s, SPACE_ID_2, "Дом", "h@x.ru")
        e_work = await _mail(s, work, "Договор аренды")
        e_home = await _mail(s, home, "Договор на дачу")
        await s.commit()
        # A label (global) on both threads: the listing shows each space its own mail.
        both = [e_work.thread_id, e_home.thread_id]
        assert [e.id for e in await views.list_mail(s, "inbox", thread_ids=both,
                                                     space_id=SPACE_ID)] == [e_work.id]
        assert [e.id for e in await views.list_mail(s, "inbox", thread_ids=both,
                                                     space_id=SPACE_ID_2)] == [e_home.id]
        assert await views.count_mail(s, "inbox", thread_ids=both, space_id=SPACE_ID) == 1
        # Search is scoped the same way: a space only ever sees its own mail.
        from email_reader.search import index as search_index
        from email_reader.search import query as search_query

        await search_index.reconcile(s)
        _, here = await search_query.search(s, "договор", space_id=SPACE_ID)
        _, there = await search_query.search(s, "договор", space_id=SPACE_ID_2)
        _, other_word = await search_query.search(s, "аренды", space_id=SPACE_ID_2)
        assert [r.ref for r in here] == [e_work.id.hex]
        assert [r.ref for r in there] == [e_home.id.hex]
        assert other_word == []


def test_switcher_lands_only_on_screens_every_space_has():
    assert safe_next("/view/needreply") == "/view/needreply"
    assert safe_next("/meetings?filter=unrouted") == "/meetings"
    assert safe_next("/people") == "/people"
    assert safe_next("/settings/section/prompts") == "/settings/section/prompts"
    for foreign in ("/project/abc", "/meeting/abc", "/mail/account/abc/inbox", "/item/abc",
                    "", "http://evil/", "/view/../x"):
        assert safe_next(foreign) == "/view/today", foreign


async def test_default_space_appears_on_demand_and_a_new_mailbox_lands_in_it(db):
    async with db() as s:
        assert await spaces_svc.list_spaces(s) == []
        a = await accounts_svc.create_account(
            s, name="A", provider="imap", host="h", port=993, login="a@x.ru", password="p",
            folders=["INBOX"],
        )
        await s.commit()
        spaces = await spaces_svc.list_spaces(s)
        assert [sp.name for sp in spaces] == [spaces_svc.DEFAULT_NAME]
        assert a.space_id == spaces[0].id


async def test_a_space_is_deleted_only_when_empty_and_never_the_last_one(db):
    async with db() as s:
        await mk_space(s)
        other = await mk_space(s, 2)
        a = await _mailbox(s, SPACE_ID, "Работа", "w@x.ru")
        await s.commit()
        assert "ящики" in await spaces_svc.delete_blocker(s, SPACE_ID)
        assert await spaces_svc.delete_blocker(s, other.id) is None
        # Moving the mailbox empties space 1; its items stay where they are (in space 1).
        await accounts_svc.update_account(s, a.id, space_id=other.id)
        await s.commit()
        assert await spaces_svc.delete_blocker(s, SPACE_ID) is None
        assert await spaces_svc.delete_space(s, SPACE_ID) is None
        await s.commit()
        assert [sp.id for sp in await spaces_svc.list_spaces(s)] == [other.id]
        assert "ящики" in await spaces_svc.delete_blocker(s, other.id)
        await accounts_svc.delete_account(s, a.id)
        await s.commit()
        assert "Последнее" in await spaces_svc.delete_blocker(s, other.id)


async def test_the_mailbox_chip_hides_other_mail_not_hand_made_work(db):
    """Задача, набранная руками, за собой письма не имеет — и под чипом «Ящик» исчезала
    ровно в тот момент, когда её создали, хотя тост обещал «Мои задачи», а бейдж в меню
    (у него этого фильтра нет) продолжал её считать. Чип отсеивает ЧУЖОЙ ящик."""
    async with db() as s:
        space = await mk_space(s)
        work = await _mailbox(s, space.id, "work", "me@work.ru")
        other = await _mailbox(s, space.id, "Второй", "me@second.ru")
        mine = await _mail(s, work, "смета")
        theirs = await _mail(s, other, "договор")

        s.add_all([
            ActionItem(type=ActionType.TASK, title="набрана руками", status="open",
                       space_id=space.id),
            ActionItem(type=ActionType.MY_COMMITMENT, title="сказано на встрече",
                       status="open", space_id=space.id),
            ActionItem(type=ActionType.TASK, title="из письма work", status="open",
                       space_id=space.id, source_thread_id=mine.thread_id),
            ActionItem(type=ActionType.TASK, title="из письма другого ящика", status="open",
                       space_id=space.id, source_thread_id=theirs.thread_id),
        ])
        await s.flush()

        q = views._view_query_scoped("commitments", space.id, work.id)
        titles = {i.title for i in (await s.execute(q)).scalars()}

    assert "набрана руками" in titles
    assert "сказано на встрече" in titles
    assert "из письма work" in titles
    assert "из письма другого ящика" not in titles
