"""Stage 3 of docs/ux-tasks.md: grouping axes, undo of a resolving action, the row's date
popover and bulk actions."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from email_reader import views
from email_reader.enums import ActionStatus, ActionType, SourceKind, UserVerdict
from email_reader.models import (
    Account,
    ActionItem,
    EvalSample,
    Meeting,
    Project,
    ProjectMeeting,
    ProjectThread,
    Thread,
)
from tests.conftest import SPACE_ID, mk_space


async def _account(s, name="A"):
    a = Account(space_id=SPACE_ID, name=name, host="h", login=f"{name}@ya.ru", folders=[])
    s.add(a)
    await s.flush()
    return a


async def test_group_by_person_orders_groups_by_urgency_and_puts_nobody_last(db):
    async with db() as s:
        await mk_space(s)
        s.add_all([
            ActionItem(
                space_id=SPACE_ID, type=ActionType.DELEGATED_TASK, title="anon", status="open"),
            ActionItem(
                space_id=SPACE_ID, type=ActionType.DELEGATED_TASK, title="b-late", status="open",
                       counterparty="Боря", due_date=date.today() + timedelta(days=9)),
            ActionItem(
                space_id=SPACE_ID, type=ActionType.DELEGATED_TASK, title="a-soon", status="open",
                       counterparty="Аня", due_date=date.today() + timedelta(days=1)),
            ActionItem(
                space_id=SPACE_ID, type=ActionType.DELEGATED_TASK, title="b-soon", status="open",
                       counterparty="Боря", due_date=date.today() + timedelta(days=2)),
            ActionItem(space_id=SPACE_ID, type=ActionType.TO_DELEGATE, title="team", status="open",
                       delegate_to="Подрядчик"),
        ])
        await s.commit()
        b = await views.board(s, "delegated", group="person")
        # The TO_DELEGATE row groups by the team it goes to; «Без контрагента» closes.
        assert b.sections == [("Подрядчик", 1), ("Аня", 1), ("Боря", 2), ("Без контрагента", 1)]
        assert [r.item.title for r in b.rows] == ["team", "a-soon", "b-soon", "b-late", "anon"]


async def test_group_by_project_and_source(db):
    async with db() as s:
        await mk_space(s)
        acc = await _account(s)
        th = Thread(account_id=acc.id)
        m = Meeting(space_id=SPACE_ID, ext_id="m1", title="Планёрка", started_at=datetime.now(UTC))
        p = Project(space_id=SPACE_ID, name="Альфа")
        s.add_all([th, m, p])
        await s.flush()
        s.add_all([ProjectThread(project_id=p.id, thread_id=th.id),
                   ProjectMeeting(project_id=p.id, meeting_id=m.id)])
        s.add_all([
            ActionItem(
                space_id=SPACE_ID, type=ActionType.MY_COMMITMENT, title="mail", status="open",
                       source_thread_id=th.id, counterparty="x"),
            ActionItem(
                space_id=SPACE_ID, type=ActionType.TASK, title="spoken", status="open",
                source_meeting_id=m.id),
            ActionItem(space_id=SPACE_ID, type=ActionType.TASK, title="typed", status="open",
                       source_kind=SourceKind.MANUAL),
        ])
        await s.commit()
        by_project = await views.board(s, "commitments", group="project")
        # Группа без Нити зовётся «Без нити» с 19.09 (Проект → Нить на экранах).
        assert by_project.sections == [("Альфа", 2), ("Без нити", 1)]
        by_source = await views.board(s, "commitments", group="source")
        assert sorted(by_source.sections) == sorted(
            [("Планёрка", 1), ("Добавлено вручную", 1), ("Без источника", 1)]
        )  # the mail item has no EmailOccurrence in this fixture -> no mailbox to name
        assert by_source.sections[-1] == ("Без источника", 1)


async def test_undo_restores_a_completed_and_a_dismissed_item(db):
    async with db() as s:
        await mk_space(s)
        acc = await _account(s)
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()
        ai = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="t", status="open",
                        source_thread_id=th.id, counterparty="Иван")
        s.add(ai)
        await s.commit()
        prev = views.snapshot(ai)
        assert prev == {"status": "open", "next_check": None, "resolved_by": None,
                        "user_edited": False, "user_verdict": None}

        await views.complete(s, ai.id)
        await s.commit()
        assert ai.status == ActionStatus.DONE and ai.resolved_by == "user"
        assert await views.restore(s, ai.id, prev)
        await s.commit()
        assert (ai.status, ai.resolved_by) == (ActionStatus.OPEN, None)

        await views.dismiss(s, ai.id)
        await s.commit()
        n_before = (await s.execute(select(func.count()).select_from(EvalSample))).scalar_one()
        assert ai.user_verdict == UserVerdict.REJECTED
        assert await views.restore(s, ai.id, prev)
        await s.commit()
        assert ai.status == ActionStatus.OPEN and ai.user_verdict is None
        # The dismissal had snapshotted the thread without the item; the undo appends a
        # corrected sample on top.
        n_after = (await s.execute(select(func.count()).select_from(EvalSample))).scalar_one()
        assert n_after == n_before + 1
        assert [i.title for i in await views.list_view(s, "needreply")] == ["t"]


async def test_restore_rejects_garbage(db):
    async with db() as s:
        await mk_space(s)
        ai = ActionItem(space_id=SPACE_ID, type=ActionType.TASK, title="t", status="open")
        s.add(ai)
        await s.commit()
        assert not await views.restore(s, ai.id, {"status": "banana"})
        assert not await views.restore(s, ai.id, {"status": "open", "user_verdict": "nope"})
        assert not await views.restore(s, ai.id, {"status": "open", "next_check": "yesterday"})
        assert not await views.restore(s, ai.id, "not a dict")


async def test_set_due_lifts_overdue_and_clears(db):
    async with db() as s:
        await mk_space(s)
        ai = ActionItem(
            space_id=SPACE_ID, type=ActionType.TASK, title="t", status=ActionStatus.OVERDUE,
                        due_date=date.today() - timedelta(days=3))
        s.add(ai)
        await s.commit()
        await views.set_due(s, ai.id, date.today() + timedelta(days=2))
        await s.commit()
        assert ai.status == ActionStatus.OPEN and ai.user_edited
        await views.set_due(s, ai.id, None)
        await s.commit()
        assert ai.due_date is None
        # A date still in the past keeps the overdue status.
        ai.status = ActionStatus.OVERDUE
        await views.set_due(s, ai.id, date.today() - timedelta(days=1))
        await s.commit()
        assert ai.status == ActionStatus.OVERDUE


async def test_bulk_applies_one_action_to_many(db):
    async with db() as s:
        await mk_space(s)
        items = [
            ActionItem(space_id=SPACE_ID, type=ActionType.TASK, title=f"t{i}", status="open")
            for i in range(4)
        ]
        bot = await views.create_task(s, title="bot", space_id=SPACE_ID,
                                      source_kind=SourceKind.TELEGRAM, confirmed=False)
        s.add_all(items)
        await s.commit()
        ids = [i.id for i in items]
        assert await views.bulk(s, ids[:2], "complete") == 2
        assert await views.bulk(s, ids[2:3], "snooze", days=5) == 1
        assert await views.bulk(s, ids[3:], "retype", new_type="MY_COMMITMENT") == 1
        assert await views.bulk(s, [bot.id], "confirm") == 1
        await s.commit()
        assert [i.status for i in items] == ["done", "done", "snoozed", "open"]
        assert items[3].type == ActionType.MY_COMMITMENT
        assert items[2].next_check == date.today() + timedelta(days=5)
        assert bot.user_verdict == UserVerdict.CONFIRMED
        try:
            await views.bulk(s, ids, "retype")
        except ValueError as exc:
            assert "type" in str(exc)
        else:
            raise AssertionError("retype without a type must fail")
