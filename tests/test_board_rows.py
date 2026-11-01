"""Boards as rows: sections, waiting age, the delegated board's two types, and the
display-only reason filter. See docs/ux-tasks.md §4.2–4.3."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from email_reader import views
from email_reader.api.common import _list_reason, _since
from email_reader.enums import ActionStatus, ActionType
from email_reader.models import Account, ActionItem, Meeting, Thread
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space


async def test_drop_on_delegated_keeps_the_item_on_a_board(db):
    """Reproduces the lost card: dragging a «Нужен ответ» row onto «Делегировано» retypes it
    to DELEGATED_TASK (ui.js VIEW_TYPE). With no due date that type used to be on no board."""
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@ya.ru", folders=[])
        s.add(acc)
        await s.flush()
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()
        ai = ActionItem(
            space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="answer", status="open",
                        counterparty="Иван", source_thread_id=th.id)
        s.add(ai)
        await s.commit()

        async def boards_with(item_id):
            out = []
            for v in ("today", "needreply", "waiting", "delegated", "commitments"):
                if item_id in {i.id for i in await views.list_view(s, v)}:
                    out.append(v)
            return out

        assert await boards_with(ai.id) == ["today", "needreply"]
        await views.set_type(s, ai.id, "DELEGATED_TASK")
        await s.commit()
        assert await boards_with(ai.id) == ["delegated"]


async def test_delegated_board_orders_sections_then_deadline_then_wait(db):
    async with db() as s:
        await mk_space(s)
        now = datetime.now(UTC)
        s.add_all([
            ActionItem(
                space_id=SPACE_ID, type=ActionType.DELEGATED_TASK, title="waiting 20d",
                status="open",
                       created_at=now - timedelta(days=20)),
            ActionItem(
                space_id=SPACE_ID, type=ActionType.DELEGATED_TASK, title="waiting 3d",
                status="open",
                       created_at=now - timedelta(days=3)),
            ActionItem(
                space_id=SPACE_ID, type=ActionType.DELEGATED_TASK, title="due soon", status="open",
                       due_date=date.today() + timedelta(days=1),
                       created_at=now - timedelta(days=1)),
            ActionItem(
                space_id=SPACE_ID, type=ActionType.TO_DELEGATE, title="hand off", status="open",
                       created_at=now),
        ])
        await s.commit()
        b = await views.board(s, "delegated")
        # TO_DELEGATE section first; inside «Ждут исполнения»: a deadline beats no deadline,
        # then the longest wait first.
        titles = [r.item.title for r in b.rows]
        assert titles == ["hand off", "due soon", "waiting 20d", "waiting 3d"]
        assert b.rows[2].since == (now - timedelta(days=20)).date()
        assert b.rows[0].since is None  # TO_DELEGATE waits for nobody yet


async def test_meeting_delegated_task_respects_the_space(db):
    """Rewritten for spaces (docs/spaces.md §1): a task spoken at a meeting belongs to the
    meeting's space and to no other. A mailbox chip inside the space does NOT hide it: the
    chip drops another mailbox's mail, and a meeting came through no mailbox at all."""
    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        work = Account(space_id=SPACE_ID, name="w", host="h", login="w@ya.ru", folders=[])
        s.add(work)
        await s.flush()
        m = Meeting(space_id=SPACE_ID, ext_id="m1",
                    started_at=datetime.now(UTC) - timedelta(days=9))
        s.add(m)
        await s.flush()
        s.add(ActionItem(space_id=SPACE_ID, type=ActionType.DELEGATED_TASK, title="spoken",
                         status="open", source_meeting_id=m.id))
        await s.commit()

        mine = await views.list_view(s, "delegated", space_id=SPACE_ID)
        assert [i.title for i in mine] == ["spoken"]
        assert await views.list_view(s, "delegated", space_id=SPACE_ID_2) == []
        chipped = await views.list_view(
            s, "delegated", space_id=SPACE_ID, account_id=work.id
        )
        assert [i.title for i in chipped] == ["spoken"]
        b = await views.board(s, "delegated", space_id=SPACE_ID)
        assert b.rows[0].since == m.started_at.date()  # anchored on the meeting, not the row


async def test_waiting_board_splits_stuck_from_recent(db):
    async with db() as s:
        await mk_space(s)
        now = datetime.now(UTC)
        s.add_all([
            ActionItem(
                space_id=SPACE_ID, type=ActionType.WAITING_FOR_REPLY, title="fresh", status="open",
                       created_at=now - timedelta(days=2)),
            ActionItem(
                space_id=SPACE_ID, type=ActionType.WAITING_FOR_REPLY, title="stuck", status="open",
                       created_at=now - timedelta(days=12)),
        ])
        await s.commit()
        b = await views.board(s, "waiting")
        assert [(r.item.title, r.section) for r in b.rows] == [
            ("stuck", "Висит дольше недели"), ("fresh", "Недавно"),
        ]
        assert b.sections == [("Висит дольше недели", 1), ("Недавно", 1)]


async def test_today_sections_and_sidebar_count_agree(db):
    async with db() as s:
        await mk_space(s)
        s.add_all([
            ActionItem(
                space_id=SPACE_ID, type=ActionType.MY_COMMITMENT, title="late", status="open",
                       due_date=date.today() - timedelta(days=1)),
            ActionItem(space_id=SPACE_ID, type=ActionType.MY_COMMITMENT, title="now", status="open",
                       due_date=date.today()),
            ActionItem(
                space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="reply", status="open"),
            ActionItem(
                space_id=SPACE_ID, type=ActionType.WAITING_FOR_REPLY, title="woke",
                status=ActionStatus.SNOOZED,
                       next_check=date.today()),
        ])
        await s.commit()
        b = await views.board(s, "today")
        assert [(r.item.title, r.section) for r in b.rows] == [
            ("late", "Просрочено"), ("now", "Сегодня"), ("woke", "Проснулись"),
            ("reply", "Нужен ответ"),
        ]
        assert b.total == (await views.counts(s))["today"] == 4


async def test_flat_boards_have_no_sections(db):
    async with db() as s:
        await mk_space(s)
        s.add(ActionItem(
            space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="c", status="open"))
        s.add(ActionItem(
            space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="d", status=ActionStatus.DONE))
        await s.commit()
        for view in ("needreply", "done"):
            b = await views.board(s, view)
            assert b.sections == [] and b.rows[0].section is None


def test_list_reason_strips_timecodes_for_display_only():
    assert _list_reason("[00:23:10] Иванов обещал смету") == "Иванов обещал смету"
    assert _list_reason("[00:23:10]") == ""
    assert _list_reason("[1:02:03] [00:05] дважды") == "дважды"
    assert _list_reason("без тайм-кода") == "без тайм-кода"
    assert _list_reason(None) == ""


def test_since_label():
    today = datetime.now(UTC).date()
    assert _since(today - timedelta(days=12)) == "висит 12д"
    assert _since(today) == ""
    assert _since(None) == ""


async def test_a_board_dates_itself_by_the_reference_time_it_was_given(db):
    """`board(now=…)` must classify against that moment, not the wall clock — otherwise the
    briefing, which is assembled for a given day, ages its cards by today's date instead."""
    ref = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    async with db() as s:
        await mk_space(s)
        s.add_all([
            ActionItem(space_id=SPACE_ID, type=ActionType.WAITING_FOR_REPLY, title="свежее",
                       status=ActionStatus.OPEN, created_at=ref - timedelta(days=1)),
            ActionItem(space_id=SPACE_ID, type=ActionType.WAITING_FOR_REPLY, title="давнее",
                       status=ActionStatus.OPEN, created_at=ref - timedelta(days=10)),
            ActionItem(space_id=SPACE_ID, type=ActionType.MY_COMMITMENT, title="срок в тот день",
                       status=ActionStatus.OPEN, due_date=date(2026, 3, 2)),
        ])
        await s.commit()
        rows = {r.item.title: r.section for r in (await views.board(s, "waiting", SPACE_ID,
                                                                   now=ref)).rows}
        assert rows["давнее"] == "Висит дольше недели"
        assert rows["свежее"] == "Недавно"
        today = await views.board(s, "today", SPACE_ID, now=ref)
        assert [r.item.title for r in today.rows] == ["срок в тот день"]
        assert today.rows[0].section == "Сегодня"
        # …and without it the same board is dated by the real clock: that deadline is past.
        assert (await views.board(s, "today", SPACE_ID)).rows[0].section == "Просрочено"
