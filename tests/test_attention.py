"""Состояние обязательства и свёртка по Нити (docs/feature-topics.md §4.2, §8.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from email_reader import app_prefs, attention, views
from email_reader.enums import ActionStatus, ActionType, Direction
from email_reader.models import (
    Account,
    ActionItem,
    Email,
    Project,
    ProjectThread,
    Thread,
)
from tests.conftest import SPACE_ID, mk_space

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
TODAY = NOW.date()
TH = attention.Thresholds(remind_days=14, idle_days=30)


def _item(type_, *, quiet: int = 0, due: date | None = None, status=ActionStatus.OPEN):
    stamp = NOW - timedelta(days=quiet)
    return ActionItem(id=uuid.uuid4(), space_id=SPACE_ID, type=str(type_), title="t",
                      status=status, due_date=due, updated_at=stamp, created_at=stamp)


def _state(type_, **kw):
    it = _item(type_, **kw)
    return attention.state_of(it, it.updated_at, TODAY, TH)


def test_two_axes_side_and_freshness():
    # Нужен ответ: на мне всегда и не стареет никогда.
    s = _state(ActionType.NEED_MY_REPLY, quiet=90)
    assert (s.side, s.fresh, s.on_me, s.quiet_days) == ("reply", "flow", True, 90)
    # Моя сторона: без срока — не «на мне», через порог — затухла.
    s = _state(ActionType.MY_COMMITMENT, quiet=40)
    assert (s.side, s.fresh, s.on_me) == ("mine", "idle", False)
    assert _state(ActionType.MY_COMMITMENT, quiet=2, due=TODAY).on_me is True
    assert _state(ActionType.TASK, quiet=2, due=TODAY - timedelta(days=1)).on_me is True
    assert _state(ActionType.TO_DELEGATE, quiet=2, status=ActionStatus.OVERDUE).on_me
    # Чужой ход проходит «пора напомнить» по дороге в «затухло» и никогда не «на мне».
    assert _state(ActionType.WAITING_FOR_REPLY, quiet=5).fresh == "flow"
    s = _state(ActionType.WAITING_FOR_REPLY, quiet=15)
    assert (s.side, s.fresh, s.on_me) == ("theirs", "remind", False)
    assert _state(ActionType.DELEGATED_TASK, quiet=31).fresh == "idle"
    assert _state(ActionType.DELEGATED_TASK, quiet=31, due=TODAY).on_me is False
    # Без стороны — не участвует.
    assert _state(ActionType.DECISION, quiet=200).side == "none"
    assert _state(ActionType.DECISION, quiet=200).fresh == "flow"


def test_zero_thresholds_switch_the_rules_off():
    off = attention.Thresholds(remind_days=0, idle_days=0)
    it = _item(ActionType.WAITING_FOR_REPLY, quiet=400)
    assert attention.state_of(it, it.updated_at, TODAY, off).fresh == "flow"
    only_idle = attention.Thresholds(remind_days=0, idle_days=30)
    assert attention.state_of(it, it.updated_at, TODAY, only_idle).fresh == "idle"


def test_fold_takes_the_most_urgent_state_first():
    st = lambda side, fresh, on_me=False: attention.CardState(  # noqa: E731
        side, fresh, on_me, 0, NOW)
    assert attention.fold([]) == "quiet"
    assert attention.fold([st("mine", "idle"), st("reply", "flow", True)]) == "on_me"
    assert attention.fold([st("mine", "idle"), st("theirs", "idle")]) == "idle"
    # Одна не затухшая — Нить не затухла, но напомнить пора.
    assert attention.fold([st("mine", "idle"), st("theirs", "remind")]) == "remind"
    assert attention.fold([st("theirs", "flow"), st("mine", "flow")]) == "wait"
    assert attention.fold([st("mine", "flow")]) == "flow"
    # Решения не стареют и не считаются «всеми стареющими».
    assert attention.fold([st("none", "flow")]) == "flow"


async def test_a_letter_in_the_conversation_is_the_last_movement(db):
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@b.ru", folders=[])
        s.add(acc)
        await s.flush()
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()
        s.add(Email(account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
                    direction=Direction.OUTBOUND, from_addr="me@b.ru", to_addrs=[],
                    cc_addrs=[], sent_at=NOW - timedelta(days=2)))
        card = _item(ActionType.MY_COMMITMENT, quiet=40)
        card.source_thread_id = th.id
        lonely = _item(ActionType.MY_COMMITMENT, quiet=40)
        s.add_all([card, lonely])
        await s.flush()
        states = await attention.states_for(s, [card, lonely], now=NOW, th=TH)
    assert states[card.id].quiet_days == 2 and states[card.id].fresh == "flow"
    assert states[lonely.id].quiet_days == 40 and states[lonely.id].fresh == "idle"


async def _threads(s):
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@b.ru", folders=[])
    s.add(acc)
    await s.flush()
    made = {}
    plan = {
        "На мне": (ActionType.NEED_MY_REPLY, 3),
        "Напомнить": (ActionType.WAITING_FOR_REPLY, 20),
        "Затухла": (ActionType.DELEGATED_TASK, 40),
        "Идёт": (ActionType.MY_COMMITMENT, 1),
        "Тихая": None,
    }
    for name, spec in plan.items():
        p = Project(space_id=SPACE_ID, name=name, prompt="")
        th = Thread(account_id=acc.id)
        s.add_all([p, th])
        await s.flush()
        s.add(ProjectThread(project_id=p.id, thread_id=th.id, source="manual"))
        if spec:
            type_, quiet = spec
            it = _item(type_, quiet=quiet)
            it.source_thread_id = th.id
            s.add(it)
        made[name] = p
    await s.commit()
    return made


async def test_for_threads_folds_each_thread_and_orders_by_urgency(db):
    async with db() as s:
        made = await _threads(s)
        rows = await attention.for_threads(s, SPACE_ID, now=NOW)
        everything = await attention.for_threads(s, SPACE_ID, now=NOW, include_quiet=True)
        one = await attention.for_thread(s, made["Напомнить"], now=NOW)
    assert [(t.name, t.state) for t in rows] == [
        ("На мне", "on_me"), ("Затухла", "idle"), ("Напомнить", "remind"), ("Идёт", "flow"),
    ]
    assert [t.name for t in everything][-1] == "Тихая"
    assert rows[0].project_id == made["На мне"].id and rows[0].label == "на мне"
    assert (one.state, one.theirs, one.remind, one.quiet_days, one.css) == (
        "remind", 1, 1, 20, "remind")
    assert attention.ThreadAttention.from_dict(one.as_dict()) == one


async def test_board_rows_carry_the_remind_mark(db):
    async with db() as s:
        await mk_space(s)
        s.add_all([_item(ActionType.WAITING_FOR_REPLY, quiet=20),
                   _item(ActionType.WAITING_FOR_REPLY, quiet=2)])
        await s.commit()
        board = await views.board(s, "waiting", space_id=SPACE_ID, now=NOW)
    marks = sorted((r.remind for r in board.rows), key=lambda v: (v is None, v))
    assert marks == [20, None]


async def test_remind_days_is_a_setting_with_a_default(db):
    async with db() as s:
        await mk_space(s)
        assert await app_prefs.get_remind_days(s) == 14
        await app_prefs.set_remind_days(s, 7)
        th = await attention.thresholds(s)
    assert (th.remind_days, th.idle_days) == (7, 30)


async def test_a_direct_link_beats_the_conversation_and_reaches_the_thread(db):
    """Карточка без беседы (Telegram, вручную) попадает в Нить только прямой привязкой;
    карточка с беседой и прямой привязкой считается только у той Нити, что в привязке
    (docs/feature-topics.md §8.3, projects.card_member)."""
    from email_reader import projects as projects_svc

    async with db() as s:
        made = await _threads(s)
        moved = made["Идёт"]  # у неё уже есть карточка через беседу
        lonely = _item(ActionType.TASK, quiet=1)  # без источника вовсе
        lonely.project_id = made["Тихая"].id
        # Карточка беседы Нити «Идёт», привязанная прямо к «Тихой»: уходит из «Идёт».
        stolen = _item(ActionType.MY_COMMITMENT, quiet=1)
        stolen.source_thread_id = (await s.execute(
            select(ProjectThread.thread_id).where(ProjectThread.project_id == moved.id)
        )).scalar_one()
        stolen.project_id = made["Тихая"].id
        s.add_all([lonely, stolen])
        await s.commit()
        rows = {t.name: t for t in await attention.for_threads(s, SPACE_ID, now=NOW)}
        members = (await s.execute(
            select(ActionItem.id).where(projects_svc.card_member(made["Тихая"].id))
        )).scalars().all()
    assert rows["Тихая"].open_cards == 2 and rows["Тихая"].state == "flow"
    assert rows["Идёт"].open_cards == 1
    assert set(members) == {lonely.id, stolen.id}


async def test_a_snoozed_thread_rests_until_its_date_and_the_actions_journal(db):
    async with db() as s:
        made = await _threads(s)
        idle = made["Затухла"]
        await attention.snooze(s, idle, TODAY + timedelta(days=3))
        await s.commit()
        resting = await attention.for_thread(s, idle, now=NOW)
        later = await attention.for_thread(s, idle, now=NOW + timedelta(days=3))
        # «Продолжать» — отсчёт заново, «отложена» снимается.
        kept = await attention.keep(s, idle, now=NOW)
        await s.commit()
        after_keep = await attention.for_thread(s, idle, now=NOW)
        # «Снять с досок» — снятие, не закрытие.
        dropped = await attention.dismiss_open(s, made["Напомнить"], now=NOW)
        await s.commit()
        gone = await attention.for_thread(s, made["Напомнить"], now=NOW)
        card = (await s.execute(
            select(ActionItem).where(ActionItem.status == ActionStatus.DISMISSED)
        )).scalar_one()
        from email_reader import history

        story = [e.text for e in await history.for_item(s, card.id)]
    assert (resting.state, resting.label) == ("snoozed", "отложена")
    assert later.state == "idle"
    # Карточка «Затухлой» — чужой ход: после «Продолжать» она свежая, значит Нить «жду».
    assert kept == 1 and after_keep.state == "wait" and idle.snoozed_until is None
    assert dropped == 1 and gone.state == "quiet"
    assert card.resolved_by == "user" and card.user_verdict == "rejected"
    assert story[-1] == "Снята с досок вместе с затухшей Нитью"
