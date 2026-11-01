"""Board order: section, then VIP sender, then soonest deadline, then age.
One key for every commitment board — list_view and next_open_item share it."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from email_reader import views
from email_reader.enums import ActionStatus, ActionType
from email_reader.models import Account, ActionItem, Email, Thread
from tests.conftest import SPACE_ID, mk_space

TODAY = date(2026, 7, 4)


def _item(type_, *, due=None, next_check=None, thread=None, created=None):
    return ActionItem(space_id=SPACE_ID, 
        type=type_, title="t", status=ActionStatus.OPEN, due_date=due, next_check=next_check,
        source_thread_id=thread, created_at=created or datetime(2026, 7, 1, tzinfo=UTC),
    )


def test_waiting_since_anchors_meeting_items_on_the_meeting():
    """A mail row survives re-extraction, so created_at is a stable anchor. A meeting row is
    deleted and re-created on every re-run of the recording, so its anchor is the meeting."""
    mtg_id = uuid.uuid4()
    meeting = type("M", (), {"started_at": datetime(2026, 6, 10, 9, 0, tzinfo=UTC)})()
    mail = _item(ActionType.DELEGATED_TASK, created=datetime(2026, 7, 1, tzinfo=UTC))
    spoken = _item(ActionType.DELEGATED_TASK, created=datetime(2026, 7, 1, tzinfo=UTC))
    spoken.source_meeting_id = mtg_id
    assert views.waiting_since(mail, {}) == date(2026, 7, 1)
    assert views.waiting_since(spoken, {mtg_id: meeting}) == date(2026, 6, 10)
    # Only «their court» types wait for anyone.
    assert views.waiting_since(_item(ActionType.MY_COMMITMENT), {}) is None


def test_today_sections_overdue_today_woken_reply():
    over = _item(ActionType.MY_COMMITMENT, due=date(2026, 7, 1))       # past due, not yet ticked
    over.status = ActionStatus.OPEN
    marked = _item(ActionType.MY_COMMITMENT, due=date(2026, 6, 1))
    marked.status = ActionStatus.OVERDUE
    today = _item(ActionType.MY_COMMITMENT, due=TODAY)
    woke = _item(ActionType.WAITING_FOR_REPLY)
    woke.status = ActionStatus.SNOOZED
    # Snoozed by hand with a deadline that has since passed: the snooze wins, it is
    # «woke up», not «overdue» — the user parked it knowingly.
    woke_late = _item(ActionType.MY_COMMITMENT, due=date(2026, 6, 1))
    woke_late.status = ActionStatus.SNOOZED
    reply = _item(ActionType.NEED_MY_REPLY)
    sec = lambda i: views.section_index("today", i, TODAY, None)  # noqa: E731
    assert [sec(over), sec(marked), sec(today), sec(woke), sec(woke_late), sec(reply)] == [
        0, 0, 1, 2, 2, 3,
    ]


def test_waiting_sections_split_on_a_week():
    it = _item(ActionType.WAITING_FOR_REPLY)
    assert views.section_index("waiting", it, TODAY, TODAY - timedelta(days=7)) == 0
    assert views.section_index("waiting", it, TODAY, TODAY - timedelta(days=6)) == 1
    assert views.section_index("waiting", it, TODAY, None) == 1


def test_order_key_section_then_vip_then_deadline_then_age():
    vip_t = uuid.uuid4()
    vip_far = _item(ActionType.MY_COMMITMENT, due=date(2026, 12, 1), thread=vip_t)
    due_soon = _item(ActionType.MY_COMMITMENT, due=date(2026, 7, 5))
    due_late = _item(ActionType.MY_COMMITMENT, due=date(2026, 7, 20))
    # Same deadline (none), differ only by age → older first.
    old = _item(ActionType.NEED_MY_REPLY, created=datetime(2026, 6, 1, tzinfo=UTC))
    new = _item(ActionType.NEED_MY_REPLY, created=datetime(2026, 6, 20, tzinfo=UTC))

    items = [due_late, new, vip_far, due_soon, old]
    items.sort(key=lambda i: views.order_key(0, i, {vip_t}, None))
    # VIP first (despite December); then by deadline; the two undated replies older→newer.
    assert items == [vip_far, due_soon, due_late, old, new]
    # A later section always sorts after an earlier one, whatever the deadline.
    assert views.order_key(1, vip_far, {vip_t}, None) > views.order_key(0, due_late, set(), None)


async def test_next_open_item_follows_then_falls_back(db):
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@ya.ru", folders=[])
        s.add(acc)
        await s.flush()
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()

        def mk(title, days):
            return ActionItem(space_id=SPACE_ID, 
                type=ActionType.MY_COMMITMENT, title=title, status=ActionStatus.OPEN,
                due_date=date.today() + timedelta(days=days), source_thread_id=th.id,
            )

        a, b, c = mk("a", 0), mk("b", 1), mk("c", 2)  # board order by due: a, b, c
        s.add_all([a, b, c])
        await s.commit()
        # After a middle item → the following one; after the last → the previous one.
        assert (await views.next_open_item(s, "commitments", None, None, b.id)).title == "c"
        assert (await views.next_open_item(s, "commitments", None, None, c.id)).title == "b"
        # Only one item left → nothing to advance to.
        a.status = ActionStatus.DONE
        c.status = ActionStatus.DONE
        await s.commit()
        assert await views.next_open_item(s, "commitments", None, None, b.id) is None


async def test_list_view_floats_vip_across_the_board(db):
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@ya.ru", folders=[])
        s.add(acc)
        await s.flush()
        th_vip, th_norm = Thread(account_id=acc.id), Thread(account_id=acc.id)
        s.add_all([th_vip, th_norm])
        await s.flush()

        def _mail(th, frm):
            return Email(
                account_id=acc.id, thread_id=th.id, direction="inbound", from_addr=frm,
                to_addrs=[{"email": "me@ya.ru"}], cc_addrs=[], dedup_hash=frm,
                sent_at=datetime.now(UTC), subject="s", body_text="b",
            )

        def _commit(title, days, thread):
            return ActionItem(space_id=SPACE_ID, 
                type=ActionType.MY_COMMITMENT, title=title, status=ActionStatus.OPEN,
                due_date=date.today() + timedelta(days=days), source_thread_id=thread.id,
            )

        s.add_all([_mail(th_vip, "boss@acme.ru"), _mail(th_norm, "joe@x.ru")])
        # A far-future VIP commitment must still outrank a due-today normal one.
        s.add_all([
            _commit("later", 5, th_norm),
            _commit("today", 0, th_norm),
            _commit("vip", 30, th_vip),
        ])
        await s.commit()

        from email_reader import vip
        await vip.set_raw(s, "acme.ru")  # domain entry → boss@acme.ru is VIP
        await s.commit()

        titles = [i.title for i in await views.list_view(s, "commitments")]
    assert titles == ["vip", "today", "later"]
