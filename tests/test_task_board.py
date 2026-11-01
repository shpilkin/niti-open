"""TASK type, «Мои задачи» sections, quick-add ownership and the «Разобрать» inbox."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select

from email_reader import meetings, views
from email_reader.ai.schemas import MeetingItem
from email_reader.enums import ActionType, SourceKind, UserVerdict
from email_reader.models import ActionItem, Meeting
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space


async def test_my_tasks_board_splits_promises_from_tasks_to_self(db):
    async with db() as s:
        await mk_space(s)
        s.add_all([
            ActionItem(space_id=SPACE_ID, type=ActionType.TASK, title="self", status="open"),
            ActionItem(
                space_id=SPACE_ID, type=ActionType.MY_COMMITMENT, title="promised", status="open",
                       counterparty="Иван"),
        ])
        await s.commit()
        b = await views.board(s, "commitments")
        assert [(r.item.title, r.section) for r in b.rows] == [
            ("promised", "Обещал"), ("self", "Себе"),
        ]


async def test_quick_add_task_is_confirmed_owned_and_dated(db):
    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        ai = await views.create_task(s, title="оплатить счёт", due=date.today(), space_id=SPACE_ID)
        await s.commit()
        assert ai.source_kind == SourceKind.MANUAL
        assert ai.user_verdict == UserVerdict.CONFIRMED and ai.user_edited
        # Due today -> also on «Сегодня»; owned by space 1, invisible in space 2.
        today = await views.list_view(s, "today", space_id=SPACE_ID)
        assert [i.title for i in today] == ["оплатить счёт"]
        assert await views.list_view(s, "commitments", space_id=SPACE_ID_2) == []
        assert await views.list_view(s, "triage") == []  # nothing to triage: the user typed it


async def test_bot_items_wait_in_triage_until_ruled_on(db):
    async with db() as s:
        await mk_space(s)
        ai = await views.create_task(
            s, title="из телеги", space_id=SPACE_ID, source_kind=SourceKind.TELEGRAM,
            source_ref="1:2", confirmed=False,
        )
        await s.commit()
        assert [i.title for i in await views.list_view(s, "triage")] == ["из телеги"]
        assert (await views.counts(s))["triage"] == 1
        # Not on any board yet — a due date must not smuggle it into «Сегодня» either.
        ai.due_date = date.today()
        await s.commit()
        for v in ("today", "commitments", "needreply", "waiting", "delegated"):
            assert await views.list_view(s, v) == [], v
        # A snooze is not a verdict: it stays in the inbox, dimmed.
        await views.snooze(s, ai.id, days=3)
        await s.commit()
        assert [i.title for i in await views.list_view(s, "triage")] == ["из телеги"]
        # Accepting it moves it onto its type's board and out of the inbox.
        await views.confirm(s, ai.id)
        await s.commit()
        assert await views.list_view(s, "triage") == []
        assert [i.title for i in await views.list_view(s, "commitments")] == []  # snoozed
        assert [i.title for i in await views.list_view(s, "snoozed")] == ["из телеги"]


async def test_completing_an_unruled_bot_item_archives_it(db):
    """«Готово» straight from the bot's button (or the inbox row) sets no verdict. It must
    still count as a ruling: out of «Разобрать» AND into the archive — not into limbo."""
    async with db() as s:
        await mk_space(s)
        ai = await views.create_task(s, title="из телеги", space_id=SPACE_ID,
                                     source_kind=SourceKind.TELEGRAM, confirmed=False)
        await s.commit()
        await views.complete(s, ai.id)
        await s.commit()
        assert await views.list_view(s, "triage") == []
        assert [i.title for i in await views.list_view(s, "done")] == ["из телеги"]
        # A bulk «Готово» on the inbox goes the same way; ids that vanished are not counted.
        bot2 = await views.create_task(s, title="ещё", space_id=SPACE_ID,
                                       source_kind=SourceKind.TELEGRAM, confirmed=False)
        await s.commit()
        assert await views.bulk(s, [bot2.id, uuid.uuid4()], "complete") == 1
        await s.commit()
        assert {i.title for i in await views.list_view(s, "done")} == {"из телеги", "ещё"}


async def test_triage_respects_the_space(db):
    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        await views.create_task(s, title="для A", space_id=SPACE_ID,
                                source_kind=SourceKind.TELEGRAM, confirmed=False)
        await s.commit()
        assert [i.title for i in await views.list_view(s, "triage", space_id=SPACE_ID)] == ["для A"]
        assert await views.list_view(s, "triage", space_id=SPACE_ID_2) == []


async def test_meeting_promise_without_a_person_becomes_a_task(db):
    async with db() as s:
        await mk_space(s)
        m = Meeting(space_id=SPACE_ID, ext_id="m1", started_at=datetime.now(UTC))
        s.add(m)
        await s.flush()
        stats = await meetings._sync_items(s, m, [
            MeetingItem(type=ActionType.MY_COMMITMENT, title="Сделать выгрузку"),
            MeetingItem(type=ActionType.MY_COMMITMENT, title="Прислать смету",
                        counterparty="Иванов"),
            MeetingItem(type=ActionType.TASK, title="Проверить логи"),
        ])
        await s.commit()
        assert stats["items_created"] == 3
        rows = {r.title: r.type for r in await views.list_view(s, "commitments")}
        assert rows == {
            "Сделать выгрузку": ActionType.TASK,      # promised to nobody -> task to self
            "Прислать смету": ActionType.MY_COMMITMENT,
            "Проверить логи": ActionType.TASK,
        }


async def test_rerouting_a_meeting_keeps_a_card_linked_to_a_thread(db):
    """Переразбор встречи пересоздаёт её машинные карточки, но привязанную к Нити —
    владельцем или принятым предложением агента — не трогает."""
    from email_reader.models import Project

    async with db() as s:
        await mk_space(s)
        m = Meeting(space_id=SPACE_ID, ext_id="m1", started_at=datetime.now(UTC))
        thread = Project(name="Альфа", space_id=SPACE_ID, prompt="", aliases=[])
        s.add_all([m, thread])
        await s.flush()
        await meetings._sync_items(s, m, [
            MeetingItem(type=ActionType.TASK, title="Проверить логи"),
            MeetingItem(type=ActionType.TASK, title="Сделать выгрузку"),
        ])
        linked = (await s.execute(select(ActionItem).where(
            ActionItem.title == "Проверить логи"))).scalar_one()
        linked.project_id = thread.id
        await s.flush()
        await meetings._sync_items(s, m, [MeetingItem(type=ActionType.TASK, title="Новая")])
        titles = {r.title: r.project_id for r in (await s.execute(select(ActionItem))).scalars()}
    assert titles == {"Проверить логи": thread.id, "Новая": None}
