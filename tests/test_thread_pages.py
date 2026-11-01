"""Экраны Нити: создание одним кликом, чипы в панели обязательства, страница и список
(docs/feature-topics.md §8.5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

import email_reader.api.app  # noqa: F401 — registers icon()/filters on the template env
from email_reader import attention, views
from email_reader import projects as projects_svc
from email_reader.api.common import templates
from email_reader.enums import ActionStatus, ActionType, Direction
from email_reader.models import (
    Account,
    ActionItem,
    Email,
    Meeting,
    Project,
    ProjectMeeting,
    ProjectThread,
    Thread,
)
from tests.conftest import SPACE_ID, mk_space

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


async def _mailbox(s):
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@b.ru", folders=[])
    s.add(acc)
    await s.flush()
    return acc


async def test_a_thread_is_made_from_a_conversation_a_meeting_and_a_card(db):
    async with db() as s:
        acc = await _mailbox(s)
        th = Thread(account_id=acc.id, subject_norm="договор")
        s.add(th)
        await s.flush()
        s.add(Email(account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
                    direction=Direction.INBOUND, from_addr="p@r.ru", to_addrs=[], cc_addrs=[],
                    subject="Re: Договор с «Ромашкой»", sent_at=NOW))
        m = Meeting(ext_id="m1", space_id=SPACE_ID, started_at=NOW, title="Планёрка")
        card = ActionItem(space_id=SPACE_ID, type=ActionType.TASK, title="Купить билеты",
                          status=ActionStatus.OPEN, source_kind="manual")
        s.add_all([m, card])
        await s.flush()

        from_thread = await projects_svc.create_from_thread(s, space_id=SPACE_ID, thread_id=th.id)
        again = await projects_svc.create_from_thread(s, space_id=SPACE_ID, thread_id=th.id)
        from_meeting = await projects_svc.create_from_meeting(s, m)
        from_card = await projects_svc.create_from_card(s, card)
        await s.commit()

        linked = (await s.execute(
            select(ProjectThread.project_id).where(ProjectThread.thread_id == th.id)
        )).scalars().all()
        meeting_link = (await s.execute(
            select(ProjectMeeting).where(ProjectMeeting.meeting_id == m.id)
        )).scalar_one()
    # Имя — тема беседы; второй раз имя занято, значит «(2)»; встреча и карточка — по своим.
    assert from_thread.name == "Re: Договор с «Ромашкой»" and from_thread.include_meetings
    assert again.name == "Re: Договор с «Ромашкой» (2)"
    assert set(linked) == {from_thread.id, again.id}
    assert from_meeting.name == "Планёрка" and meeting_link.source == "manual"
    assert from_card.name == "Купить билеты" and card.project_id == from_card.id


async def test_card_thread_chips_tell_direct_from_inherited(db):
    async with db() as s:
        acc = await _mailbox(s)
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()
        via = Project(space_id=SPACE_ID, name="Через беседу", prompt="")
        other = Project(space_id=SPACE_ID, name="Другая", prompt="")
        s.add_all([via, other])
        await s.flush()
        s.add(ProjectThread(project_id=via.id, thread_id=th.id, source="auto"))
        card = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="Ответить",
                          status=ActionStatus.OPEN, source_thread_id=th.id)
        s.add(card)
        await s.commit()
        projects = await projects_svc.list_projects(s, SPACE_ID)
        inherited = set((await views.projects_for_items(s, [card])).get(card.id, []))
        html = templates.env.get_template("_card_threads.html").render(
            item=card, projects=projects, inherited=inherited)
        card.project_id = other.id
        await s.commit()
        html_direct = templates.env.get_template("_card_threads.html").render(
            item=card, projects=projects, inherited=set())
        names = (await views.projects_for_items(s, [card])).get(card.id)
    assert 'title="Через беседу или встречу"' in html and "Через беседу" in html
    assert "+ Другая" in html and "Новая нить" in html
    # Прямая привязка: чип с крестиком, унаследованный больше не горит, группировка
    # доски видит только прямую Нить.
    assert 'title="Привязано прямо"' in html_direct and "Снять привязку" in html_direct
    assert "+ Через беседу" in html_direct
    assert names == ["Другая"]


async def test_the_thread_page_context_groups_cards_by_side(db):
    """Страница Нити: «На мне», «Жду» с пометкой «пора напомнить», «Мои обещания»."""
    async with db() as s:
        acc = await _mailbox(s)
        th = Thread(account_id=acc.id)
        p = Project(space_id=SPACE_ID, name="Нить", prompt="")
        s.add_all([th, p])
        await s.flush()
        s.add(ProjectThread(project_id=p.id, thread_id=th.id, source="manual"))
        stamp = NOW - timedelta(days=20)
        for type_, title in ((ActionType.NEED_MY_REPLY, "ответить"),
                             (ActionType.WAITING_FOR_REPLY, "жду"),
                             (ActionType.MY_COMMITMENT, "обещал")):
            s.add(ActionItem(space_id=SPACE_ID, type=type_, title=title,
                             status=ActionStatus.OPEN, source_thread_id=th.id,
                             updated_at=stamp, created_at=stamp))
        await s.commit()
        cards = await attention.cards_of(s, p, now=NOW)
        head = await attention.for_thread(s, p, now=NOW)
    by = {i.title: st for i, st in cards}
    assert by["ответить"].on_me and by["жду"].fresh == "remind" and by["обещал"].side == "mine"
    assert head.state == "on_me" and head.theirs == 1 and head.remind == 1


async def test_candidates_block_shows_the_agents_case_and_asks_for_a_verdict(db):
    async with db() as s:
        await mk_space(s)
        pr = await projects_svc.propose(
            s, space_id=SPACE_ID, title="Переезд склада", why="восемь писем и встреча",
            evidence=[{"kind": "meeting", "id": str(uuid.uuid4()), "title": "Продуктовая"},
                      {"kind": "card", "id": str(uuid.uuid4()), "title": "Оценить API"}],
            by="Claude Code",
        )
        await s.commit()
        proposed = await projects_svc.list_proposed(s, SPACE_ID)
        html = templates.env.get_template("_thread_candidates.html").render(proposed=proposed)
        assert await projects_svc.list_projects(s, SPACE_ID) == []
    assert "Кандидаты — 1" in html and "восемь писем и встреча" in html
    assert "предложил Claude Code" in html and "Продуктовая" in html and "Оценить API" in html
    assert f'action="/project/{pr.id}/verdict"' in html
    assert 'value="yes"' in html and 'value="no"' in html
