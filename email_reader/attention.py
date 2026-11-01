"""Состояние обязательства и его свёртка по Нити (docs/feature-topics.md §4.2, §8.2).

Состояние считается, а не хранится: у обязательства две оси — чья сторона (тип карточки)
и свежесть (сколько дней без движения против порогов из настроек). Движение — правка
карточки или письмо в её беседе, в любую сторону; у карточек со встреч и из Telegram
беседы нет, у них движение только правка. Нить ничего своего не хранит: её состояние —
свёртка состояний открытых обязательств, поэтому «движения Нити» как понятия нет.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import app_prefs
from email_reader.enums import LIVE_STATUSES, ActionStatus, ActionType, UserVerdict
from email_reader.models import (
    ActionItem,
    AgentDecision,
    Email,
    Project,
    ProjectMeeting,
    ProjectThread,
)

# Сторона обязательства.
MINE = "mine"        # моё: обещание, передать, задача
REPLY = "reply"      # нужен ответ — тоже моё, но не стареет никогда
THEIRS = "theirs"    # чужой ход: жду ответа, передано
NONE = "none"        # без стороны: решение, справка, подготовка

# Свежесть.
FLOW = "flow"        # движение свежее
REMIND = "remind"    # чужой ход молчит дольше порога напоминания
IDLE = "idle"        # движения нет дольше порога «затухло»

# Состояние Нити — свёртка, по убыванию срочности.
ON_ME = "on_me"
WAIT = "wait"
SNOOZED = "snoozed"  # владелец отложил Нить до даты: до неё она не требует внимания
QUIET = "quiet"      # открытых обязательств нет: тихая, не затухшая
STATES = (ON_ME, IDLE, REMIND, WAIT, FLOW, SNOOZED, QUIET)
LABELS = {
    ON_ME: "на мне", IDLE: "затухла", REMIND: "пора напомнить",
    WAIT: "жду", FLOW: "идёт", SNOOZED: "отложена", QUIET: "тихая",
}
# Класс пилюли `.st-pill` (DESIGN.md): состояние → семантический модификатор.
CSS = {ON_ME: "mine", IDLE: "idle", REMIND: "remind", WAIT: "wait", FLOW: "flow",
       SNOOZED: "idle", QUIET: "idle"}

_MINE_TYPES = {ActionType.MY_COMMITMENT, ActionType.TO_DELEGATE, ActionType.TASK}
_THEIRS_TYPES = {ActionType.WAITING_FOR_REPLY, ActionType.DELEGATED_TASK}
_OPEN = [s.value for s in LIVE_STATUSES]


def side(item: ActionItem) -> str:
    t = item.type
    if t == ActionType.NEED_MY_REPLY:
        return REPLY
    if t in _MINE_TYPES:
        return MINE
    if t in _THEIRS_TYPES:
        return THEIRS
    return NONE


@dataclass(frozen=True)
class Thresholds:
    remind_days: int  # 0 — не напоминать
    idle_days: int    # 0 — не считать затухшим


async def thresholds(session: AsyncSession) -> Thresholds:
    return Thresholds(
        remind_days=await app_prefs.get_remind_days(session),
        idle_days=await app_prefs.get_idle_days(session),
    )


@dataclass
class CardState:
    side: str
    fresh: str
    on_me: bool          # требует меня сегодня: просрочено, срок ≤ сегодня, «нужен ответ»
    quiet_days: int      # дней с последнего движения
    moved_at: datetime   # последнее движение: правка карточки или письмо в беседе


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def state_of(
    item: ActionItem, moved_at: datetime, today: date, th: Thresholds
) -> CardState:
    """Две оси по одному обязательству. «Нужен ответ» — на мне всегда и не стареет; моя
    сторона стареет в «затухло»; чужой ход проходит «пора напомнить» по дороге."""
    s = side(item)
    quiet = max(0, (today - moved_at.date()).days)
    fresh = FLOW
    if s in (MINE, THEIRS) and th.idle_days > 0 and quiet >= th.idle_days:
        fresh = IDLE
    elif s == THEIRS and th.remind_days > 0 and quiet >= th.remind_days:
        fresh = REMIND
    due = item.due_date
    on_me = s == REPLY or (
        s == MINE and (item.status == ActionStatus.OVERDUE or (due is not None and due <= today))
    )
    return CardState(side=s, fresh=fresh, on_me=on_me, quiet_days=quiet, moved_at=moved_at)


async def moved_at_for(
    session: AsyncSession, items: Iterable[ActionItem]
) -> dict[uuid.UUID, datetime]:
    """Последнее движение каждой карточки: её `updated_at` или последнее письмо в
    беседе-источнике, что позже. Письма берутся одним запросом на все беседы."""
    rows = list(items)
    tids = {i.source_thread_id for i in rows if i.source_thread_id is not None}
    last_mail: dict[uuid.UUID, datetime] = {}
    if tids:
        q = (
            select(Email.thread_id, func.max(Email.sent_at))
            .where(Email.thread_id.in_(list(tids)))
            .group_by(Email.thread_id)
        )
        for tid, at in await session.execute(q):
            if at is not None:
                last_mail[tid] = _aware(at)
    out: dict[uuid.UUID, datetime] = {}
    for i in rows:
        stamps = [_aware(i.updated_at), _aware(i.created_at)]
        if i.source_thread_id in last_mail:
            stamps.append(last_mail[i.source_thread_id])
        known = [s for s in stamps if s is not None]
        out[i.id] = max(known) if known else datetime.now(UTC)
    return out


async def states_for(
    session: AsyncSession, items: Iterable[ActionItem], *, now: datetime | None = None,
    th: Thresholds | None = None,
) -> dict[uuid.UUID, CardState]:
    rows = list(items)
    if not rows:
        return {}
    th = th or await thresholds(session)
    today = (now or datetime.now(UTC)).date()
    moved = await moved_at_for(session, rows)
    return {i.id: state_of(i, moved[i.id], today, th) for i in rows}


def fold(states: Iterable[CardState]) -> str:
    """Состояние Нити из состояний её открытых обязательств (§8.2): на мне, если есть
    хоть одно такое; иначе затухла, если затухли все стареющие; иначе пора напомнить,
    если есть хоть одно; иначе жду, если есть чужой ход; иначе идёт. Без открытых
    обязательств — тихая."""
    rows = list(states)
    if not rows:
        return QUIET
    if any(s.on_me for s in rows):
        return ON_ME
    aging = [s for s in rows if s.side in (MINE, THEIRS)]
    if aging and all(s.fresh == IDLE for s in aging):
        return IDLE
    if any(s.fresh == REMIND for s in rows):
        return REMIND
    if any(s.side == THEIRS for s in rows):
        return WAIT
    return FLOW


@dataclass
class ThreadAttention:
    """Нить как её видит брифинг и страница: состояние и счётчики, посчитанные из
    открытых обязательств. Хранится только в кэше брифинга (JSON), не в таблице."""

    project_id: uuid.UUID
    name: str
    state: str
    on_me: int = 0
    theirs: int = 0
    remind: int = 0
    idle: int = 0
    open_cards: int = 0
    quiet_days: int | None = None  # дней с последнего движения по любой карточке Нити

    @property
    def label(self) -> str:
        return LABELS[self.state]

    @property
    def css(self) -> str:
        return CSS[self.state]

    @property
    def rank(self) -> int:
        return STATES.index(self.state)

    def as_dict(self) -> dict:
        return {
            "id": str(self.project_id), "name": self.name, "state": self.state,
            "on_me": self.on_me, "theirs": self.theirs, "remind": self.remind,
            "idle": self.idle, "open": self.open_cards, "quiet": self.quiet_days,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ThreadAttention:
        return cls(
            project_id=uuid.UUID(d["id"]), name=d["name"], state=d["state"],
            on_me=int(d.get("on_me", 0)), theirs=int(d.get("theirs", 0)),
            remind=int(d.get("remind", 0)), idle=int(d.get("idle", 0)),
            open_cards=int(d.get("open", 0)),
            quiet_days=None if d.get("quiet") is None else int(d["quiet"]),
        )


def summarize(
    project: Project, states: Iterable[CardState], today: date | None = None
) -> ThreadAttention:
    rows = list(states)
    state = fold(rows)
    today = today or datetime.now(UTC).date()
    if project.snoozed_until is not None and today < project.snoozed_until and rows:
        state = SNOOZED
    return ThreadAttention(
        project_id=project.id, name=project.name, state=state,
        on_me=sum(1 for s in rows if s.on_me),
        theirs=sum(1 for s in rows if s.side == THEIRS),
        remind=sum(1 for s in rows if s.fresh == REMIND),
        idle=sum(1 for s in rows if s.fresh == IDLE),
        open_cards=len(rows),
        quiet_days=min((s.quiet_days for s in rows), default=None),
    )


async def _open_cards_by_project(
    session: AsyncSession, projects: list[Project]
) -> dict[uuid.UUID, list[ActionItem]]:
    """Открытые обязательства каждой Нити — через её беседы и встречи."""
    out: dict[uuid.UUID, list[ActionItem]] = {p.id: [] for p in projects}
    if not projects:
        return out
    ids = [p.id for p in projects]
    tmap = {}
    for pid, tid in await session.execute(
        select(ProjectThread.project_id, ProjectThread.thread_id)
        .where(ProjectThread.project_id.in_(ids))
    ):
        tmap.setdefault(tid, []).append(pid)
    mmap = {}
    for pid, mid in await session.execute(
        select(ProjectMeeting.project_id, ProjectMeeting.meeting_id)
        .where(ProjectMeeting.project_id.in_(ids))
    ):
        mmap.setdefault(mid, []).append(pid)
    # Прямая привязка сильнее источника (projects.card_member): карточка с ней считается
    # только у своей Нити, карточка без неё — у Нитей своей беседы или встречи.
    conds = [ActionItem.project_id.in_(ids)]
    if tmap:
        conds.append(ActionItem.source_thread_id.in_(list(tmap)))
    if mmap:
        conds.append(ActionItem.source_meeting_id.in_(list(mmap)))
    items = (await session.execute(
        select(ActionItem).where(ActionItem.status.in_(_OPEN), or_(*conds))
    )).scalars().all()
    for i in items:
        if side(i) == NONE:
            continue
        if i.project_id is not None:
            if i.project_id in out:
                out[i.project_id].append(i)
            continue
        for pid in tmap.get(i.source_thread_id, []) + mmap.get(i.source_meeting_id, []):
            out[pid].append(i)
    return out


async def for_threads(
    session: AsyncSession, space_id: uuid.UUID, *, now: datetime | None = None,
    include_quiet: bool = False,
) -> list[ThreadAttention]:
    """Все включённые Нити пространства по убыванию срочности; тихие — по флагу."""
    projects = list((await session.execute(
        select(Project).where(Project.space_id == space_id, Project.enabled.is_(True),
                              Project.status == "active")
        .order_by(Project.name)
    )).scalars().all())
    th = await thresholds(session)
    cards = await _open_cards_by_project(session, projects)
    every = {i.id: i for rows in cards.values() for i in rows}
    states = await states_for(session, every.values(), now=now, th=th)
    today = (now or datetime.now(UTC)).date()
    out = [summarize(p, (states[i.id] for i in cards[p.id]), today) for p in projects]
    if not include_quiet:
        out = [t for t in out if t.state != QUIET]
    return sorted(out, key=lambda t: (t.rank, -(t.on_me + t.idle + t.remind), t.name))


async def for_thread(
    session: AsyncSession, project: Project, *, now: datetime | None = None
) -> ThreadAttention:
    cards = await _open_cards_by_project(session, [project])
    states = await states_for(session, cards[project.id], now=now)
    return summarize(project, states.values(), (now or datetime.now(UTC)).date())


async def cards_of(
    session: AsyncSession, project: Project, *, now: datetime | None = None
) -> list[tuple[ActionItem, CardState]]:
    """Открытые обязательства Нити с их состояниями — для страницы Нити."""
    cards = await _open_cards_by_project(session, [project])
    states = await states_for(session, cards[project.id], now=now)
    return [(i, states[i.id]) for i in cards[project.id]]


# --- действия над Нитью (docs/feature-topics.md §8.4) -------------------------------


def _journal(item_id: uuid.UUID, **payload) -> AgentDecision:
    from email_reader.views import ORIGIN

    via = ORIGIN.get()
    return AgentDecision(kind="user_action", action_item_id=item_id,
                         payload={**payload, **({"via": via} if via else {})})


async def keep(session: AsyncSession, project: Project, *, now: datetime | None = None) -> int:
    """«Продолжать» на затухшей Нити — массовое «оставить»: у стареющих карточек отсчёт
    тишины начинается заново, предложения закрыть снимаются."""
    now = now or datetime.now(UTC)
    n = 0
    for item, st in await cards_of(session, project, now=now):
        if st.side not in (MINE, THEIRS):
            continue
        item.updated_at = now
        item.close_candidate = False
        item.close_hint = None
        session.add(_journal(item.id, action="keep_topic", project=str(project.id)))
        n += 1
    project.snoozed_until = None
    return n


async def snooze(session: AsyncSession, project: Project, until: date) -> None:
    """«Отложить до…» — одна дата на Нить; карточки свои даты не теряют."""
    project.snoozed_until = until
    session.add(AgentDecision(kind="user_action", payload={
        "action": "topic_snooze", "project": str(project.id), "until": until.isoformat(),
    }))


async def dismiss_open(
    session: AsyncSession, project: Project, *, now: datetime | None = None
) -> int:
    """«Снять с досок» — не «закрыть»: обязательства снимаются с причиной `idle_topic`,
    никто не утверждает, что они исполнены (правило `history.py`). Отмена — `reopen`."""
    n = 0
    for item, _st in await cards_of(session, project, now=now):
        item.status = ActionStatus.DISMISSED
        item.resolved_by = "user"
        item.user_edited = True
        item.user_verdict = UserVerdict.REJECTED
        item.close_candidate = False
        item.close_hint = None
        session.add(_journal(item.id, action="dismiss", reason="idle_topic",
                             project=str(project.id)))
        n += 1
    return n
