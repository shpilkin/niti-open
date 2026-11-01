"""Read models (views) + user actions on ActionItems.

Views are deterministic SQL over the DB — UI and Telegram answer from these, never
from LLM generation. Actions are FSM transitions that also feed the eval loop.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import String as sa_String
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import settings_store
from email_reader.clock import today as _today
from email_reader.enums import (
    LIVE_STATUSES,
    ActionStatus,
    ActionType,
    Direction,
    SourceKind,
    UserVerdict,
)
from email_reader.models import (
    Account,
    ActionItem,
    AgentDecision,
    CalendarEvent,
    Email,
    EmailOccurrence,
    EvalSample,
    Meeting,
    Project,
    ProjectMeeting,
    ProjectThread,
)
from email_reader.search import index as search_index

LIVE = [s.value for s in LIVE_STATUSES]

VIEWS = ("today", "triage", "needreply", "waiting", "delegated", "commitments", "snoozed", "done")


def _live_filter():
    return ActionItem.status.in_(LIVE)


def _triaged():
    """What the boards may show: anything the bot brought in stays in «Разобрать» until
    the user has ruled on it — confirm / edit / dismiss set a verdict, and «Готово» is a
    ruling too (complete() sets no verdict, so the status has to count; without this a
    completed bot item would be neither in the inbox nor in the archive)."""
    return or_(
        ActionItem.source_kind != SourceKind.TELEGRAM,
        ActionItem.user_verdict.isnot(None),
        ActionItem.status.in_([ActionStatus.DONE.value, ActionStatus.DISMISSED.value]),
    )


def _view_query(view: str, now: datetime | None = None):
    base = select(ActionItem).where(_triaged())
    if view == "triage":
        # Unruled Telegram arrivals, snoozed ones included — a snooze is not a verdict.
        return (
            select(ActionItem)
            .where(
                ActionItem.source_kind == SourceKind.TELEGRAM,
                ActionItem.user_verdict.is_(None),
                ActionItem.status.in_(LIVE + [ActionStatus.SNOOZED.value]),
            )
            .order_by(ActionItem.created_at.desc())
        )
    if view == "today":
        today = _today(now)
        return base.where(
            or_(
                and_(
                    _live_filter(),
                    or_(
                        ActionItem.status == ActionStatus.OVERDUE,
                        ActionItem.due_date <= today,
                        ActionItem.type == ActionType.NEED_MY_REPLY,
                    ),
                ),
                # Snoozed items wake up here on their next_check day.
                and_(
                    ActionItem.status == ActionStatus.SNOOZED,
                    ActionItem.next_check <= today,
                ),
            ),
        ).order_by(ActionItem.due_date.asc().nullslast(), ActionItem.created_at.desc())
    if view == "needreply":
        return base.where(_live_filter(), ActionItem.type == ActionType.NEED_MY_REPLY).order_by(
            ActionItem.created_at.desc()
        )
    if view == "waiting":
        return base.where(
            _live_filter(), ActionItem.type == ActionType.WAITING_FOR_REPLY
        ).order_by(ActionItem.created_at.asc())
    if view == "delegated":
        # Both «their court» types: work to hand off (TO_DELEGATE) and work already handed
        # off (DELEGATED_TASK). The latter had no board at all — undated ones were invisible.
        return base.where(
            _live_filter(),
            ActionItem.type.in_([ActionType.TO_DELEGATE, ActionType.DELEGATED_TASK]),
        ).order_by(ActionItem.due_date.asc().nullslast(), ActionItem.created_at.desc())
    if view == "commitments":
        # «Мои задачи»: what I promised someone (MY_COMMITMENT) and what I set myself (TASK).
        return base.where(
            _live_filter(),
            ActionItem.type.in_([ActionType.MY_COMMITMENT, ActionType.TASK]),
        ).order_by(ActionItem.due_date.asc().nullslast())
    if view == "snoozed":
        return base.where(ActionItem.status == ActionStatus.SNOOZED).order_by(
            ActionItem.next_check.asc().nullslast()
        )
    if view == "done":
        return base.where(
            ActionItem.status.in_([ActionStatus.DONE, ActionStatus.DISMISSED])
        ).order_by(ActionItem.updated_at.desc())
    raise ValueError(f"unknown view {view!r}")


def _filter_query(spec, now: datetime | None = None):
    """Запрос своей доски (docs/ux-tasks.md §17.5). Живые карточки выбранных типов, плюс
    одно условие из закрытого списка. Предложенные к закрытию уходят в конец — на этих
    досках они образуют ту же последнюю секцию, что и на пресетах."""
    today = _today(now)
    q = select(ActionItem).where(
        _triaged(), _live_filter(), ActionItem.type.in_(list(spec.types or []))
    )
    condition = spec.condition or ""
    if condition == "due_soon":
        q = q.where(
            or_(ActionItem.status == ActionStatus.OVERDUE, ActionItem.due_date <= today)
        )
    elif condition == "no_due":
        q = q.where(ActionItem.due_date.is_(None))
    elif condition == "candidate":
        q = q.where(ActionItem.close_candidate.is_(True))
    elif condition == "no_owner":
        # «Договорились действовать, но ответственного нет»: запрос на передачу, у
        # которого не сказано кому. Считается из колонок, а не флагом от планировщика —
        # снимать тут нечего, это чинится назначением (docs/feature-card-advice.md §1.8).
        q = q.where(
            ActionItem.type == ActionType.TO_DELEGATE, ActionItem.delegate_to.is_(None)
        )
    if spec.sort == "created_desc":
        chosen = [ActionItem.created_at.desc()]
    elif spec.sort == "created_asc":
        chosen = [ActionItem.created_at.asc()]
    else:
        chosen = [ActionItem.due_date.asc().nullslast(), ActionItem.created_at.desc()]
    return q.order_by(ActionItem.close_candidate.asc(), *chosen)


def _view_query_scoped(
    view: str, space_id: uuid.UUID | None, account_id: uuid.UUID | None = None,
    now: datetime | None = None, spec=None,
):
    """The board query inside one space (docs/spaces.md §1). space_id None = every space —
    only the read-only global bot and tests ask for that; the UI always has a space.

    account_id is the mailbox chip inside the space: it drops what came through ANOTHER
    mailbox. Items with no mailbox behind them — meetings, Telegram, quick-add — belong to
    the space itself (docs/spaces.md: the space owns them, mailboxes are a lens on mail),
    so the chip does not hide them. Hiding them made a typed task vanish the moment it was
    created, while the sidebar badge — which never had this filter — kept counting it."""
    from email_reader import boards as boards_svc

    q = (
        _filter_query(spec, now)
        if spec is not None and spec.kind == boards_svc.FILTER
        else _view_query(view, now)
    )
    if space_id is not None:
        q = q.where(ActionItem.space_id == space_id)
    if account_id is not None:
        thread_sub = (
            select(Email.thread_id)
            .join(EmailOccurrence, EmailOccurrence.email_id == Email.id)
            .where(EmailOccurrence.account_id == account_id)
        )
        event_sub = select(CalendarEvent.id).where(CalendarEvent.account_id == account_id)
        q = q.where(
            or_(
                ActionItem.source_thread_id.in_(thread_sub),
                ActionItem.source_event_id.in_(event_sub),
                and_(
                    ActionItem.source_thread_id.is_(None),
                    ActionItem.source_event_id.is_(None),
                ),
            )
        )
    return q


# Boards that are ordered in Python (sections, VIP, deadlines, age); snoozed/done keep
# their own SQL order and have no sections.
_ORDERED_VIEWS = frozenset({"today", "needreply", "waiting", "delegated", "commitments"})

# Types whose row means «the other side owes me» — they carry a waiting age.
_WAIT_TYPES = (ActionType.WAITING_FOR_REPLY, ActionType.DELEGATED_TASK)

# Последняя секция «своих» досок: карточки, которые ассистент предлагает закрыть. Не
# срочное, поэтому внизу; решает пользователь (docs/ux-tasks.md §17.4).
_CLOSE_SECTION = "Предлагаю закрыть"

# Section labels per board, in display order. A board absent here is flat.
_SECTIONS: dict[str, tuple[str, ...]] = {
    "today": ("Просрочено", "Сегодня", "Проснулись", "Нужен ответ"),
    "waiting": ("Висит дольше недели", "Недавно"),
    "delegated": ("Передать команде", "Ждут исполнения", _CLOSE_SECTION),
    "commitments": ("Обещал", "Себе", _CLOSE_SECTION),
}
_STUCK_DAYS = 7


def waiting_since(item: ActionItem, meetings: dict) -> date | None:
    """Day the wait for the other side began (WAITING/DELEGATED only).

    Mail items keep their row across re-extractions, so created_at is stable. Meeting
    items are deleted and re-created on every re-run of the recording
    (meetings._sync_items), which would reset «висит 21д» to zero — so for them the
    anchor is the meeting itself."""
    if item.type not in _WAIT_TYPES:
        return None
    m = meetings.get(item.source_meeting_id) if item.source_meeting_id else None
    if m is not None and m.started_at:
        return m.started_at.date()
    return item.created_at.date() if item.created_at else None


def section_index(view: str, item: ActionItem, today: date, since: date | None) -> int:
    """Index into _SECTIONS[view] for the item; 0 on flat boards."""
    if view == "today":
        # Parked by hand and woke up today: the user's snooze outranks a stale deadline.
        if item.status == ActionStatus.SNOOZED:
            return 2
        # The scheduler marks overdue every 30 min; a past due date is overdue right now.
        if item.status == ActionStatus.OVERDUE or (item.due_date and item.due_date < today):
            return 0
        if item.due_date == today:
            return 1
        return 3
    if view == "waiting":
        return 0 if since is not None and (today - since).days >= _STUCK_DAYS else 1
    if view == "delegated":
        if item.close_candidate:
            return 2
        return 0 if item.type == ActionType.TO_DELEGATE else 1
    if view == "commitments":
        if item.close_candidate:
            return 2
        return 0 if item.type == ActionType.MY_COMMITMENT else 1
    return 0


def order_key(
    section: int, item: ActionItem, vip_threads: set, since: date | None,
    memory: float = 0.0,
) -> tuple:
    """One ordering for the whole board — list_view and next_open_item share it, so
    «Готово → открыть следующее» lands on the row that is visually next: section, then
    VIP senders, then the soonest deadline, then what the sender history says (see
    memory.stats.memory_score — higher first), then the oldest wait / oldest row."""
    created = item.created_at or datetime.min.replace(tzinfo=UTC)
    return (
        section,
        0 if item.source_thread_id in vip_threads else 1,
        item.due_date or date.max,
        -memory,
        since or created.date(),
        created,
    )


@dataclass
class BoardRow:
    item: ActionItem
    section: str | None  # header label; None on flat boards
    since: date | None  # when the wait began (waiting/delegated), for «висит Nд»
    # Дней тишины, когда чужой ход молчит дольше порога «пора напомнить» (attention);
    # None — пометки нет.
    remind: int | None = None


def _remind_days(states: dict, item: ActionItem) -> int | None:
    from email_reader import attention as attention_svc

    st = states.get(item.id)
    return st.quiet_days if st is not None and st.fresh == attention_svc.REMIND else None


@dataclass
class Board:
    rows: list[BoardRow]  # the requested page
    total: int
    sections: list[tuple[str, int]]  # (label, count) over the WHOLE board; [] if flat


async def vip_threads_of(session: AsyncSession, items: list[ActionItem]) -> set:
    """Subset of the items' source threads whose (any) inbound sender is a VIP contact.
    VIP is matched by email/domain, which isn't expressible in SQL — resolve it here in
    one batch query over the threads' inbound senders."""
    from email_reader import vip

    entries = await vip.get_entries(session)
    tids = {i.source_thread_id for i in items if i.source_thread_id}
    if not entries or not tids:
        return set()
    rows = await session.execute(
        select(Email.thread_id, Email.from_addr).where(
            Email.thread_id.in_(tids), Email.direction == Direction.INBOUND
        )
    )
    return {tid for tid, addr in rows if vip.is_vip(addr or "", entries)}


async def ongoing_threads_of(session: AsyncSession, items: list[ActionItem]) -> set:
    """Threads among the items' sources that already carry a message of mine."""
    tids = {i.source_thread_id for i in items if i.source_thread_id}
    if not tids:
        return set()
    rows = await session.execute(
        select(Email.thread_id)
        .where(Email.thread_id.in_(tids), Email.direction == Direction.OUTBOUND)
        .distinct()
    )
    return set(rows.scalars().all())


async def memory_scores_for(
    session: AsyncSession, items: list[ActionItem]
) -> dict[uuid.UUID, float]:
    """item id -> memory.stats.memory_score, from the cache only: a board render must stay
    cheap, the worker keeps the cache warm (memory.stats.refresh_stale).

    Only for cards where I owe the action: the backtest label was «I wrote back», so it
    says nothing about rows where the other side owes me — those keep the oldest-wait
    order (score 0)."""
    from email_reader.memory import stats as stats_svc

    ongoing = await ongoing_threads_of(session, items)
    addr_of = {i.id: stats_svc.address_of(i.counterparty) for i in items}
    by_space: dict[uuid.UUID, set[str]] = {}
    for i in items:
        if addr_of[i.id]:
            by_space.setdefault(i.space_id, set()).add(addr_of[i.id])
    cache: dict[tuple, object] = {}
    for sid, addrs in by_space.items():
        for addr, st in (await stats_svc.cached(session, sid, addrs)).items():
            cache[(sid, addr)] = st
    return {
        i.id: (
            0.0 if i.type in _WAIT_TYPES else stats_svc.memory_score(
                cache.get((i.space_id, addr_of[i.id])), i.source_thread_id in ongoing
            )
        )
        for i in items
    }


async def next_open_item(
    session: AsyncSession, view: str, space_id: uuid.UUID | None,
    account_id: uuid.UUID | None, after_item_id: uuid.UUID,
) -> ActionItem | None:
    """The board item to open after resolving `after_item_id`: the one immediately after it
    in the board's current sort order, or the previous one if it was last, or None if it was
    the only item. Computed BEFORE the resolution, while the item is still in the list."""
    items = await list_view(
        session, view, space_id=space_id, account_id=account_id, limit=100_000
    )
    ids = [i.id for i in items]
    if after_item_id not in ids:
        return items[0] if items else None
    i = ids.index(after_item_id)
    if i + 1 < len(items):
        return items[i + 1]
    if i > 0:
        return items[i - 1]
    return None


# Alternative grouping axes a board can be switched to (docs/ux-tasks.md §4.2). The
# default («») is the board's own sections. The «nothing to group by» bucket of each axis
# always sorts last.
GROUPS = ("person", "project", "source")
_NO_GROUP = {"person": "Без контрагента", "project": "Без нити", "source": "Без источника"}


async def projects_for_items(
    session: AsyncSession, items: list[ActionItem]
) -> dict[uuid.UUID, list[str]]:
    """item_id -> names of the threads (topics) the card belongs to (sorted): its direct
    link when set, else the projects of its conversation / meeting (projects.card_member)."""
    direct = {i.project_id for i in items if i.project_id}
    by_direct: dict[uuid.UUID, str] = {}
    if direct:
        for pid, name in await session.execute(
            select(Project.id, Project.name).where(Project.id.in_(list(direct)))
        ):
            by_direct[pid] = name
    tids = [i.source_thread_id for i in items if i.source_thread_id and not i.project_id]
    mids = [i.source_meeting_id for i in items if i.source_meeting_id and not i.project_id]
    by_thread: dict[uuid.UUID, set[str]] = {}
    by_meeting: dict[uuid.UUID, set[str]] = {}
    if tids:
        rows = await session.execute(
            select(ProjectThread.thread_id, Project.name)
            .join(Project, Project.id == ProjectThread.project_id)
            .where(ProjectThread.thread_id.in_(tids))
        )
        for tid, name in rows:
            by_thread.setdefault(tid, set()).add(name)
    if mids:
        rows = await session.execute(
            select(ProjectMeeting.meeting_id, Project.name)
            .join(Project, Project.id == ProjectMeeting.project_id)
            .where(ProjectMeeting.meeting_id.in_(mids))
        )
        for mid, name in rows:
            by_meeting.setdefault(mid, set()).add(name)
    out: dict[uuid.UUID, list[str]] = {}
    for i in items:
        if i.project_id:
            if i.project_id in by_direct:
                out[i.id] = [by_direct[i.project_id]]
            continue
        names = by_thread.get(i.source_thread_id, set()) | by_meeting.get(
            i.source_meeting_id, set()
        )
        if names:
            out[i.id] = sorted(names)
    return out


async def _group_labels(
    session: AsyncSession, group: str, items: list[ActionItem]
) -> dict[uuid.UUID, str]:
    """The group each item falls into on the chosen axis."""
    if group == "person":
        out = {}
        for i in items:
            who = i.delegate_to if i.type == ActionType.TO_DELEGATE else i.counterparty
            out[i.id] = (who or "").strip() or _NO_GROUP[group]
        return out
    if group == "project":
        names = await projects_for_items(session, items)
        return {i.id: (names[i.id][0] if i.id in names else _NO_GROUP[group]) for i in items}
    if group == "source":
        srcs = await sources_for_items(session, items)
        meetings = await meetings_for_items(
            session, [i.source_meeting_id for i in items if i.source_meeting_id]
        )
        out = {}
        for i in items:
            if i.source_kind == SourceKind.MEETING:
                m = meetings.get(i.source_meeting_id)
                out[i.id] = (m.title if m and m.title else None) or "Встреча без названия"
            elif i.source_kind == SourceKind.TELEGRAM:
                out[i.id] = "Telegram"
            elif i.source_kind == SourceKind.MANUAL:
                out[i.id] = "Добавлено вручную"
            else:
                src = srcs.get(i.id)
                out[i.id] = src[1] if src else _NO_GROUP[group]
        return out
    raise ValueError(f"unknown group {group!r}")


async def board(
    session: AsyncSession, view: str, space_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    limit: int = 200, offset: int = 0, group: str | None = None,
    now: datetime | None = None,
) -> Board:
    from email_reader import boards as boards_svc

    spec = (
        await boards_svc.get_spec(session, space_id, view) if space_id is not None else None
    )
    q = _view_query_scoped(view, space_id, account_id, now, spec)
    if spec is not None and spec.kind == boards_svc.FILTER:
        items = list((await session.execute(q)).scalars().all())
        # Единственная секция, которая обобщается на любую доску: то, что ассистент
        # предлагает закрыть (§17.4). Остальное идёт без заголовка.
        offered = sum(1 for i in items if i.close_candidate)
        rows = [
            BoardRow(i, _CLOSE_SECTION if i.close_candidate else "", None) for i in items
        ]
        page = rows[offset : offset + limit] if limit else rows[offset:]
        sections = [(_CLOSE_SECTION, offered)] if offered else []
        return Board(page, len(rows), sections)
    if view not in _ORDERED_VIEWS:
        rows = list((await session.execute(q.limit(limit).offset(offset))).scalars().all())
        total = await count_view(session, view, space_id, account_id, now)
        return Board([BoardRow(i, None, None) for i in rows], total, [])
    # Fetch the whole (small) board, order in Python, then paginate — so sections, VIP
    # and deadline order hold ACROSS pages, not just within the fetched slice (VIP can't
    # be ordered in SQL; see vip_threads_of).
    items = list((await session.execute(q)).scalars().all())
    vip_threads = await vip_threads_of(session, items)
    memory = await memory_scores_for(session, items)
    meetings = await meetings_for_items(
        session, [i.source_meeting_id for i in items if i.type in _WAIT_TYPES]
    )
    from email_reader import attention as attention_svc

    states = await attention_svc.states_for(session, items, now=now)
    today = _today(now)
    labels = _SECTIONS.get(view)
    keyed = []
    for i in items:
        since = waiting_since(i, meetings)
        sec = section_index(view, i, today, since)
        keyed.append((order_key(sec, i, vip_threads, since, memory.get(i.id, 0.0)), sec, since, i))
    keyed.sort(key=lambda k: k[0])
    counts: dict[str, int] = {}
    rows = []
    if group in GROUPS:
        # Regroup along the chosen axis. Groups are ordered by their most urgent member
        # (keyed is already in board order, so first seen = most urgent); «Без …» last.
        by_item = await _group_labels(session, group, [k[3] for k in keyed])
        none_label = _NO_GROUP[group]
        order: list[str] = []
        for _, _, _, i in keyed:
            if by_item[i.id] not in order:
                order.append(by_item[i.id])
        order = [g for g in order if g != none_label] + (
            [none_label] if none_label in order else []
        )
        rank = {g: n for n, g in enumerate(order)}
        keyed.sort(key=lambda k: rank[by_item[k[3].id]])  # stable: board order inside a group
        for _, _, since, i in keyed:
            label = by_item[i.id]
            counts[label] = counts.get(label, 0) + 1
            rows.append(BoardRow(i, label, since, _remind_days(states, i)))
        page = rows[offset : offset + limit] if limit else rows[offset:]
        return Board(page, len(rows), [(g, counts[g]) for g in order])
    for _, sec, since, i in keyed:
        label = labels[sec] if labels else None
        if label:
            counts[label] = counts.get(label, 0) + 1
        rows.append(BoardRow(i, label, since, _remind_days(states, i)))
    page = rows[offset : offset + limit] if limit else rows[offset:]
    sections = [(lbl, counts[lbl]) for lbl in (labels or ()) if lbl in counts]
    return Board(page, len(rows), sections)


async def boards_with(
    session: AsyncSession, item: ActionItem, now: datetime | None = None
) -> list[tuple[str, str]]:
    """Доски пространства, на которых карточка сейчас видна: [(ключ, имя), …].

    Нужна тому, кто карточку только что создал или изменил и должен сказать, где её
    искать: связь «тип и срок → доска» неочевидна. Задача со сроком на понедельник
    сегодня лежит в «Моих задачах», а вовсе не в «Сегодня», и сказать про «Сегодня»
    значит отправить человека смотреть не туда.
    """
    from email_reader import boards as boards_svc

    out: list[tuple[str, str]] = []
    for spec in await boards_svc.list_specs(session, item.space_id):
        q = _view_query_scoped(spec.key, item.space_id, now=now, spec=spec)
        found = await session.execute(q.where(ActionItem.id == item.id).limit(1))
        if found.scalars().first() is not None:
            out.append((spec.key, spec.name))
    return out


async def list_view(
    session: AsyncSession, view: str, limit: int = 200, space_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None, offset: int = 0,
) -> list[ActionItem]:
    b = await board(session, view, space_id, account_id, limit, offset)
    return [r.item for r in b.rows]


async def count_view(
    session: AsyncSession, view: str, space_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None, now: datetime | None = None, spec=None,
) -> int:
    q = _view_query_scoped(view, space_id, account_id, now, spec).order_by(None)
    return int(await session.scalar(select(func.count()).select_from(q.subquery())) or 0)


async def weekly_review(session: AsyncSession, space_id: uuid.UUID | None = None) -> dict:
    """Weekly retrospective: what got done, what's stuck, what's overdue — in one space."""
    week_ago = datetime.now(UTC) - timedelta(days=7)
    in_space = ActionItem.space_id == space_id if space_id is not None else True
    done = list(
        (
            await session.execute(
                select(ActionItem)
                .where(in_space)
                .where(ActionItem.status == ActionStatus.DONE, ActionItem.updated_at >= week_ago)
                .order_by(ActionItem.updated_at.desc())
                .limit(30)
            )
        ).scalars().all()
    )
    stuck = list(
        (
            await session.execute(
                select(ActionItem)
                .where(in_space)
                .where(
                    ActionItem.status.in_(LIVE),
                    ActionItem.type.in_(
                        [ActionType.WAITING_FOR_REPLY, ActionType.DELEGATED_TASK]
                    ),
                    ActionItem.created_at < week_ago,
                )
                .order_by(ActionItem.created_at.asc())
                .limit(20)
            )
        ).scalars().all()
    )
    overdue = list(
        (
            await session.execute(
                select(ActionItem)
                .where(in_space)
                .where(ActionItem.status == ActionStatus.OVERDUE)
                .order_by(ActionItem.due_date.asc().nullslast())
                .limit(20)
            )
        ).scalars().all()
    )
    return {"done": done, "stuck": stuck, "overdue": overdue}


async def person_dossier(
    session: AsyncSession, q: str, space_id: uuid.UUID | None
) -> dict:
    """Dossier for a counterparty (name or email): open items + recent correspondence.

    Scoped to one space: everything the space knows about the person, from all of its
    mailboxes, meetings and bots — and nothing from another space (docs/spaces.md §1).
    Matching reuses the calendar gate's token matcher, so «Пётр Петров» finds «Пётр»."""
    from email_reader.people import attendee_terms, matches_counterparty

    q = (q or "").strip()
    if not q:
        return {"items": [], "emails": []}
    terms = attendee_terms({"email": q} if "@" in q else {"name": q})

    # Snoozed included: deferred is still outstanding with this person.
    items_q = select(ActionItem).where(
        ActionItem.status.in_([*LIVE, ActionStatus.SNOOZED.value])
    )
    if space_id is not None:
        items_q = items_q.where(ActionItem.space_id == space_id)
    items = [
        it
        for it in (await session.execute(items_q)).scalars().all()
        if matches_counterparty(it.counterparty, terms)
    ]

    mail_q = select(Email)
    if space_id is not None:
        mail_q = mail_q.where(
            Email.account_id.in_(select(Account.id).where(Account.space_id == space_id))
        )
    if "@" in q:
        addr = q.lower()
        mail_q = mail_q.where(
            or_(
                func.lower(Email.from_addr) == addr,
                func.lower(func.cast(Email.to_addrs, sa_String)).like(f"%{addr}%"),
            )
        )
    else:
        mail_q = mail_q.where(Email.from_name.ilike(f"%{q}%"))
    emails = list(
        (await session.execute(mail_q.order_by(Email.sent_at.desc()).limit(10)))
        .scalars()
        .all()
    )
    return {"items": items, "emails": emails}


async def person_dossier_ids(
    session: AsyncSession,
    *,
    addresses: list[str],
    names: list[str],
    space_id: uuid.UUID | None,
    limit: int = 10,
) -> dict:
    """Dossier for a person given by every identity they are known under: open items,
    recent correspondence and the meetings they were in.

    The identity list is what makes a merged profile whole — mail found by one address,
    obligations named after another, a meeting that only ever said the name. Scoped to one
    space (docs/spaces.md §1); the profile behind it is global, the evidence is not."""
    from email_reader.people import attendee_terms, matches_counterparty

    addresses = [a for a in {(a or "").strip().lower() for a in addresses} if a]
    names = [n for n in {(n or "").strip() for n in names} if n]
    if not addresses and not names:
        return {"items": [], "emails": [], "meetings": []}

    terms: set[str] = set()
    for a in addresses:
        terms |= attendee_terms({"email": a})
    for n in names:
        terms |= attendee_terms({"name": n})

    items_q = select(ActionItem).where(
        ActionItem.status.in_([*LIVE, ActionStatus.SNOOZED.value])
    )
    if space_id is not None:
        items_q = items_q.where(ActionItem.space_id == space_id)
    items = [
        it
        for it in (await session.execute(items_q)).scalars().all()
        if matches_counterparty(it.counterparty, terms)
    ]

    emails: list[Email] = []
    if addresses or names:
        mail_q = select(Email)
        if space_id is not None:
            mail_q = mail_q.where(
                Email.account_id.in_(select(Account.id).where(Account.space_id == space_id))
            )
        clauses = []
        for addr in addresses:
            clauses.append(func.lower(Email.from_addr) == addr)
            clauses.append(
                func.lower(func.cast(Email.to_addrs, sa_String)).like(f"%{addr}%")
            )
        for name in names:
            clauses.append(Email.from_name.ilike(f"%{name}%"))
        emails = list(
            (
                await session.execute(
                    mail_q.where(or_(*clauses)).order_by(Email.sent_at.desc()).limit(limit)
                )
            ).scalars().all()
        )

    # Meetings: the ones that produced a card with this person, plus the ones whose
    # participant list names them. Few enough per space to filter in Python.
    meet_q = select(Meeting).where(Meeting.deleted_at.is_(None))
    if space_id is not None:
        meet_q = meet_q.where(Meeting.space_id == space_id)
    from_items = {it.source_meeting_id for it in items if it.source_meeting_id}
    meetings = [
        m
        for m in (await session.execute(meet_q.order_by(Meeting.started_at.desc()))).scalars()
        if m.id in from_items
        or any(matches_counterparty(str(p), terms) for p in (m.participants or []))
    ][:limit]
    return {"items": items, "emails": emails, "meetings": meetings}


async def meetings_for_items(session: AsyncSession, meeting_ids: list) -> dict:
    """Map meeting_id -> Meeting, so a card extracted from a recording can name and
    link its source the way a mail card names its mailbox."""
    ids = [m for m in meeting_ids if m]
    if not ids:
        return {}
    rows = await session.execute(select(Meeting).where(Meeting.id.in_(ids)))
    return {m.id: m for m in rows.scalars().all()}


async def accounts_for_threads(
    session: AsyncSession, thread_ids: list
) -> dict[uuid.UUID, tuple[uuid.UUID, str]]:
    """Map thread_id -> (account_id, account_name) of the mailbox it came from."""
    ids = [t for t in thread_ids if t]
    if not ids:
        return {}
    rows = await session.execute(
        select(Email.thread_id, Account.id, Account.name)
        .join(EmailOccurrence, EmailOccurrence.email_id == Email.id)
        .join(Account, Account.id == EmailOccurrence.account_id)
        .where(Email.thread_id.in_(ids))
    )
    out: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
    for tid, aid, aname in rows:
        out.setdefault(tid, (aid, aname))
    return out


async def sources_for_items(
    session: AsyncSession, items: list[ActionItem]
) -> dict[uuid.UUID, tuple[uuid.UUID, str]]:
    """Map item_id -> (account_id, account_name) through the thread, for mail items.
    Items without a thread (meetings, Telegram, quick-add) have no mailbox: their source
    icon speaks for itself."""
    by_thread = await accounts_for_threads(
        session, [i.source_thread_id for i in items if i.source_thread_id]
    )
    return {
        i.id: by_thread[i.source_thread_id]
        for i in items if i.source_thread_id in by_thread
    }


def _scoped_to_space(q, space_id: uuid.UUID | None):
    """Метки и беседы глобальны, а список — нет: чужое пространство сюда попасть не должно."""
    if space_id is None:
        return q
    return q.where(
        Email.account_id.in_(select(Account.id).where(Account.space_id == space_id))
    )


async def _mail_query(
    session: AsyncSession,
    box: str,
    thread_ids: list | None,
    account_id: uuid.UUID | None,
    space_id: uuid.UUID | None = None,
    email_ids: list | None = None,
):
    """Build the filtered (unordered, unlimited) Email query for a mailbox listing.

    One mailbox when account_id is given; all mailboxes of a space when only space_id
    is. `email_ids` — фильтр по метке (метка стоит на письме), `thread_ids` — раскрытие
    беседы. Returns None when either is an explicit empty set (nothing to show).
    """
    q = select(Email)
    if email_ids is not None:
        if not email_ids:
            return None
        q = _scoped_to_space(q.where(Email.id.in_(email_ids)), space_id)
    elif thread_ids is not None:
        if not thread_ids:
            return None
        q = _scoped_to_space(q.where(Email.thread_id.in_(thread_ids)), space_id)
    elif box == "sent":
        q = q.where(Email.direction.in_([Direction.OUTBOUND, Direction.SELF]))
        if account_id is not None:
            acc = (
                await session.execute(select(Account).where(Account.id == account_id))
            ).scalar_one_or_none()
            if acc is not None:
                q = q.where(func.lower(Email.from_addr) == acc.login.lower())
        elif space_id is not None:
            q = q.where(
                func.lower(Email.from_addr).in_(
                    select(func.lower(Account.login)).where(Account.space_id == space_id)
                )
            )
    else:
        q = q.where(Email.direction == Direction.INBOUND)
        if account_id is not None:
            # Received mail "is in" an account when it has an occurrence there.
            q = q.join(EmailOccurrence, EmailOccurrence.email_id == Email.id).where(
                EmailOccurrence.account_id == account_id
            )
        elif space_id is not None:
            q = q.join(EmailOccurrence, EmailOccurrence.email_id == Email.id).where(
                EmailOccurrence.account_id.in_(
                    select(Account.id).where(Account.space_id == space_id)
                )
            )
    return q


async def list_mail(
    session: AsyncSession,
    box: str,
    limit: int = 100,
    thread_ids: list | None = None,
    account_id: uuid.UUID | None = None,
    offset: int = 0,
    space_id: uuid.UUID | None = None,
    email_ids: list | None = None,
) -> list[Email]:
    """Raw mailbox listing. box='inbox' -> received; box='sent' -> sent by me.

    When thread_ids is given (label filter), show the whole correspondence for those
    threads regardless of direction. When account_id is given, restrict to that account:
    received mail by occurrence, sent mail by sender address (so a just-sent local copy
    appears immediately, before the server's Sent-folder copy syncs back).
    """
    q = await _mail_query(session, box, thread_ids, account_id, space_id, email_ids)
    if q is None:
        return []
    q = q.order_by(Email.sent_at.desc()).distinct().limit(limit).offset(offset)
    return list((await session.execute(q)).scalars().all())


# Группировка почты по переписке — как «Organize by Conversation» в классических
# клиентах. Выключена по умолчанию: включается в Настройках → Общие.
GROUP_MAIL_KEY = "mail.group_by_thread"


async def mail_grouped(session: AsyncSession) -> bool:
    return (await settings_store.get_value(session, GROUP_MAIL_KEY)) == "1"


def _thread_key():
    """Ключ переписки. Письмо без треда — сама себе переписка, иначе все такие письма
    склеились бы в одну строку под общим NULL."""
    return func.coalesce(Email.thread_id, Email.id)


@dataclass(frozen=True)
class ThreadInfo:
    """Что показывает строка-переписка: сколько в ней писем и сколько непрочитанных."""

    count: int
    unread: int


async def list_mail_threads(
    session: AsyncSession,
    box: str,
    limit: int = 100,
    thread_ids: list | None = None,
    account_id: uuid.UUID | None = None,
    offset: int = 0,
    space_id: uuid.UUID | None = None,
    email_ids: list | None = None,
) -> tuple[list[Email], dict[uuid.UUID, ThreadInfo]]:
    """То же, что list_mail, но одна строка на переписку: последнее письмо в ней.

    Возвращает письма и сводку по переписке: размер — счётчик рядом с темой, число
    непрочитанных — жирная строка. Строка считается прочитанной, когда прочитаны все
    письма беседы, а не только последнее: иначе точка висит над раскрытой и вычитанной
    группой. Считается по тем же письмам, что показывает ящик: в «Отправленных» у
    переписки из пяти писем своих может быть два, и «5» было бы неправдой."""
    q = await _mail_query(session, box, thread_ids, account_id, space_id, email_ids)
    if q is None:
        return [], {}
    ids = q.with_only_columns(Email.id).distinct().subquery()
    ranked = (
        select(
            Email.id.label("id"),
            func.row_number()
            .over(partition_by=_thread_key(), order_by=Email.sent_at.desc())
            .label("rn"),
            func.count().over(partition_by=_thread_key()).label("n"),
            func.sum(
                case((Email.read_at.is_(None), 1), else_=0)
            ).over(partition_by=_thread_key()).label("unread"),
        )
        .where(Email.id.in_(select(ids.c.id)))
        .subquery()
    )
    rows = (
        await session.execute(
            select(Email, ranked.c.n, ranked.c.unread)
            .join(ranked, Email.id == ranked.c.id)
            .where(ranked.c.rn == 1)
            .order_by(Email.sent_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return (
        [e for e, _, _ in rows],
        {e.id: ThreadInfo(count=n, unread=int(unread or 0)) for e, n, unread in rows},
    )


async def count_mail_threads(
    session: AsyncSession,
    box: str,
    thread_ids: list | None = None,
    account_id: uuid.UUID | None = None,
    space_id: uuid.UUID | None = None,
    email_ids: list | None = None,
) -> int:
    q = await _mail_query(session, box, thread_ids, account_id, space_id, email_ids)
    if q is None:
        return 0
    ids = q.with_only_columns(Email.id).distinct().subquery()
    return int((await session.execute(
        select(func.count(func.distinct(_thread_key()))).where(
            Email.id.in_(select(ids.c.id))
        )
    )).scalar() or 0)


async def count_mail(
    session: AsyncSession,
    box: str,
    thread_ids: list | None = None,
    account_id: uuid.UUID | None = None,
    space_id: uuid.UUID | None = None,
    email_ids: list | None = None,
) -> int:
    q = await _mail_query(session, box, thread_ids, account_id, space_id, email_ids)
    if q is None:
        return 0
    sub = q.with_only_columns(Email.id).distinct().subquery()
    return int(await session.scalar(select(func.count()).select_from(sub)) or 0)


async def list_project_mail(
    session: AsyncSession, project_id: uuid.UUID, limit: int = 200
) -> list[Email]:
    """All emails of threads assigned to a project, newest first."""
    from email_reader.models import ProjectThread

    q = (
        select(Email)
        .join(ProjectThread, ProjectThread.thread_id == Email.thread_id)
        .where(ProjectThread.project_id == project_id)
        .order_by(Email.sent_at.desc())
        .distinct()
        .limit(limit)
    )
    return list((await session.execute(q)).scalars().all())


async def get_email(session: AsyncSession, email_id: uuid.UUID) -> Email | None:
    return (
        await session.execute(select(Email).where(Email.id == email_id))
    ).scalar_one_or_none()


async def agent_status(session: AsyncSession) -> dict:
    """Queue health for the extraction agent: threads waiting / failed to analyze."""
    from email_reader.enums import EventStatus, EventType
    from email_reader.models import Event

    pending = (
        await session.execute(
            select(func.count())
            .select_from(Event)
            .where(
                Event.type == EventType.THREAD_READY,
                Event.status.in_([EventStatus.PENDING, EventStatus.PROCESSING]),
            )
        )
    ).scalar_one()
    failed = (
        await session.execute(
            select(func.count())
            .select_from(Event)
            .where(Event.type == EventType.THREAD_READY, Event.status == EventStatus.FAILED)
        )
    ).scalar_one()
    # Parked because the model/infra was unavailable — counted across all model-calling
    # event types (thread analysis, project matching, meeting routing) so the whole
    # backlog is visible.
    stalled = (
        await session.execute(
            select(func.count())
            .select_from(Event)
            .where(
                Event.type.in_(
                    [EventType.THREAD_READY, EventType.PROJECT_MATCH,
                     EventType.MEETING_READY]
                ),
                Event.status == EventStatus.STALLED,
            )
        )
    ).scalar_one()
    return {"pending": pending, "failed": failed, "stalled": stalled}


async def counts(session: AsyncSession, space_id: uuid.UUID | None = None) -> dict[str, int]:
    """Board sizes for the sidebar badges — of one space, or of everything (global bot).

    Keyed by board key, so a space's own boards get their badge too (docs/ux-tasks.md
    §17.5). Без пространства (глобальный бот) считаем только пресеты: свои доски живут в
    пространстве, и складывать их между пространствами нечего."""
    from email_reader import boards as boards_svc

    out: dict[str, int] = {}
    specs = (
        {s.key: s for s in await boards_svc.list_specs(session, space_id)}
        if space_id is not None else {}
    )
    keys = list(specs) or [v for v in VIEWS]
    for key in keys:
        spec = specs.get(key)
        if (spec.kind if spec else key) == "done":
            continue
        sub = _view_query_scoped(key, space_id, spec=spec).subquery()
        out[key] = (await session.execute(select(func.count()).select_from(sub))).scalar_one()
    return out


async def get_item(session: AsyncSession, item_id: uuid.UUID) -> ActionItem | None:
    return (
        await session.execute(select(ActionItem).where(ActionItem.id == item_id))
    ).scalar_one_or_none()


@dataclass
class ThreadMessage:
    id: uuid.UUID
    is_me: bool
    who: str
    sent_at: datetime
    subject: str | None
    body: str | None
    # Есть ли у письма разметка: тогда панель показывает его так же, как почтовая —
    # в песочнице. Плоский текст ломает таблицы и списки: их не прочитать.
    has_html: bool = False
    # Full recipient lists ([{email, name}]); the template truncates + reveals inline.
    to_addrs: list = field(default_factory=list)
    cc_addrs: list = field(default_factory=list)


async def get_thread_messages(
    session: AsyncSession, thread_id: uuid.UUID
) -> list[ThreadMessage]:
    from email_reader.enums import Direction

    rows = await session.execute(
        select(Email).where(Email.thread_id == thread_id).order_by(Email.sent_at)
    )
    out = []
    for e in rows.scalars().all():
        out.append(
            ThreadMessage(
                id=e.id,
                is_me=e.direction in (Direction.OUTBOUND, Direction.SELF),
                who=e.from_name or e.from_addr,
                sent_at=e.sent_at,
                subject=e.subject,
                body=e.body_text,
                has_html=bool(e.body_raw_html),
                to_addrs=e.to_addrs or [],
                cc_addrs=e.cc_addrs or [],
            )
        )
    return out


# --- Actions (FSM transitions + eval feedback) ---


async def _record_eval(session: AsyncSession, thread_id: uuid.UUID | None) -> None:
    """Snapshot the thread's corrected live items as ground truth for the eval set."""
    if thread_id is None:
        return
    rows = await session.execute(
        select(ActionItem).where(
            ActionItem.source_thread_id == thread_id,
            ActionItem.status.in_(LIVE + [ActionStatus.DONE.value]),
            ActionItem.type != ActionType.INFO,
        )
    )
    expected = [
        {
            "type": ai.type,
            "counterparty": ai.counterparty,
            "title": ai.title,
            "due_date": ai.due_date.isoformat() if ai.due_date else None,
        }
        for ai in rows.scalars().all()
        if ai.status != ActionStatus.DISMISSED
    ]
    session.add(EvalSample(thread_id=thread_id, expected={"items": expected}))


async def _commitment_email_ids(
    session: AsyncSession, ai: ActionItem
) -> list[uuid.UUID]:
    """Emails a completed commitment should mark read. Prefer the WHOLE conversation —
    every inbound message of the thread — not just the extracted source message: the
    source is often the first message, while the reply you actually still owe (and see as
    unread) is a later one in the same thread. Falls back to the source message if the
    commitment has no thread."""
    if ai.source_thread_id is not None:
        from email_reader.enums import Direction

        ids = list(
            (
                await session.execute(
                    select(Email.id).where(
                        Email.thread_id == ai.source_thread_id,
                        Email.direction == Direction.INBOUND,
                    )
                )
            ).scalars().all()
        )
        if ids:
            return ids
    if ai.source_message_id is not None:
        return [ai.source_message_id]
    return []


# Откуда пришло действие: пусто — из приложения, «mcp» — от внешнего агента. История
# карточки должна отличать одно от другого, иначе журнал врёт о том, кто закрыл.
ORIGIN: ContextVar[str] = ContextVar("origin", default="")


def _journal(item_id: uuid.UUID, *, kind: str = "user_action", **payload) -> AgentDecision:
    via = ORIGIN.get()
    return AgentDecision(
        kind=kind, action_item_id=item_id,
        payload={**payload, **({"via": via} if via else {})},
    )


async def complete(session: AsyncSession, item_id: uuid.UUID) -> None:
    ai = await get_item(session, item_id)
    if ai:
        ai.status = ActionStatus.DONE
        ai.resolved_by = "user"  # final: automation must never reopen a user completion
        session.add(_journal(ai.id, action="complete"))
        # Completing a commitment marks the conversation's incoming mail as read — locally
        # and, via set_read's MarkSeen event, on each server it was pulled from. Do this
        # unconditionally (no "only if unread" guard): a message may already be read LOCALLY
        # but not yet flagged \Seen upstream (read before write-back existed, or on another
        # device), and pressing "Готово" must still sync the server.
        for eid in await _commitment_email_ids(session, ai):
            if await get_email(session, eid) is not None:
                await set_read(session, eid, True)


async def dismiss(session: AsyncSession, item_id: uuid.UUID) -> None:
    ai = await get_item(session, item_id)
    if ai:
        ai.status = ActionStatus.DISMISSED
        ai.resolved_by = "user"
        ai.user_edited = True
        ai.user_verdict = UserVerdict.REJECTED
        session.add(_journal(ai.id, action="dismiss"))
        await _record_eval(session, ai.source_thread_id)


async def close_duplicate(
    session: AsyncSession, item_id: uuid.UUID, target_id: uuid.UUID | None
) -> None:
    """«Закрыть как дубль»: the obligation lives on `target`, this card leaves the board.
    Neither «done» nor «not an obligation» — the eval set is not told the extraction was
    wrong, because it was not; the journal says which card kept it. Undo restores the
    same fields as a dismissal."""
    ai = await get_item(session, item_id)
    target = await get_item(session, target_id) if target_id else None
    if ai is None or target is None or target.id == ai.id:
        return
    ai.status = ActionStatus.DISMISSED
    ai.resolved_by = "user"
    ai.user_edited = True
    session.add(_journal(ai.id, action="duplicate", of=str(target.id),
                        of_title=target.title))


# Fields a resolving action (complete / dismiss / snooze) may change. An undo restores
# exactly these; what complete() did to the MAIL (marked it read, queued \\Seen) stays —
# that is a fact about the mailbox, not about the item.
_RESTORABLE = ("status", "next_check", "resolved_by", "user_edited", "user_verdict")


def snapshot(ai: ActionItem) -> dict:
    """The item's restorable state before an action, JSON-friendly (dates as ISO)."""
    out = {}
    for f in _RESTORABLE:
        v = getattr(ai, f)
        out[f] = v.isoformat() if isinstance(v, date) else v
    return out


async def restore(session: AsyncSession, item_id: uuid.UUID, prev) -> bool:
    """Undo of a resolving action: put the restorable fields back. The payload comes
    from the client (stateless undo), so every value is validated against the enums."""
    ai = await get_item(session, item_id)
    if ai is None or not isinstance(prev, dict):
        return False
    status = prev.get("status")
    if status not in {st.value for st in ActionStatus}:
        return False
    verdict = prev.get("user_verdict")
    if verdict is not None and verdict not in {v.value for v in UserVerdict}:
        return False
    nc = prev.get("next_check")
    try:
        next_check = date.fromisoformat(nc) if nc else None
    except (TypeError, ValueError):
        return False
    resolved_by = prev.get("resolved_by")
    if resolved_by is not None and not isinstance(resolved_by, str):
        return False
    was_dismissed = ai.status == ActionStatus.DISMISSED
    ai.status = status
    ai.next_check = next_check
    ai.resolved_by = resolved_by
    ai.user_edited = bool(prev.get("user_edited"))
    ai.user_verdict = verdict
    session.add(_journal(ai.id, action="undo"))
    if was_dismissed:
        # dismiss() snapshotted the thread WITHOUT this item as ground truth; record the
        # corrected state on top. The earlier sample stays — samples are append-only.
        await _record_eval(session, ai.source_thread_id)
    return True


async def set_due(session: AsyncSession, item_id: uuid.UUID, due: date | None) -> None:
    """Change (or clear) the deadline from the row. Moving a date forward lifts an OVERDUE
    back to OPEN; the scheduler would re-mark it if the new date passes too."""
    ai = await get_item(session, item_id)
    if ai is None:
        return
    ai.due_date = due
    if ai.status == ActionStatus.OVERDUE and (due is None or due >= _today()):
        ai.status = ActionStatus.OPEN
    ai.user_edited = True
    ai.user_verdict = UserVerdict.EDITED
    session.add(_journal(ai.id, action="set_due",
                        due=due.isoformat() if due else None))
    await _record_eval(session, ai.source_thread_id)


BULK_ACTIONS = ("complete", "snooze", "dismiss", "retype", "confirm")


async def bulk(
    session: AsyncSession, ids: list[uuid.UUID], action: str, *, days: int = 3,
    new_type: str | None = None,
) -> int:
    """Apply one resolving action to many items; returns how many were touched.
    No undo for bulk — the toast says how many, the archive shows which."""
    if action not in BULK_ACTIONS:
        raise ValueError(f"unknown bulk action {action!r}")
    if action == "retype" and new_type not in {t.value for t in ActionType}:
        raise ValueError("retype needs a type")
    n = 0
    for item_id in ids:
        if await get_item(session, item_id) is None:
            continue  # gone meanwhile (re-extraction, another window) — not «done» by us
        if action == "complete":
            await complete(session, item_id)
        elif action == "snooze":
            await snooze(session, item_id, days=days)
        elif action == "dismiss":
            await dismiss(session, item_id)
        elif action == "confirm":
            await confirm(session, item_id)
        else:
            await set_type(session, item_id, new_type)
        n += 1
    return n


async def snooze(
    session: AsyncSession, item_id: uuid.UUID, days: int = 3, until: date | None = None
) -> None:
    """Snooze for N days or until an explicit date (date wins when both given)."""
    ai = await get_item(session, item_id)
    if ai:
        ai.status = ActionStatus.SNOOZED
        ai.next_check = until if until and until > _today() else _today() + timedelta(days=days)
        session.add(_journal(ai.id, action="snooze",
                            until=ai.next_check.isoformat()))


async def reopen(session: AsyncSession, item_id: uuid.UUID) -> bool:
    """Вернуть закрытую, снятую или отложенную карточку на доску.

    Не `restore`: та кладёт назад состояние, которое прислал клиент вместе с тостом
    «Отменить», и через неделю восстанавливать уже нечего. Здесь состояние одно —
    открыта, и решение человеческое, поэтому `resolved_by` снимается: иначе автоматика
    будет считать карточку решённой и закроет её снова.
    """
    ai = await get_item(session, item_id)
    if ai is None or ai.status in (ActionStatus.OPEN, ActionStatus.IN_PROGRESS,
                                   ActionStatus.OVERDUE):
        return False
    was_dismissed = ai.status == ActionStatus.DISMISSED
    ai.status = ActionStatus.OPEN
    ai.next_check = None
    ai.resolved_by = None
    ai.user_edited = True
    ai.user_verdict = UserVerdict.CONFIRMED
    ai.close_candidate = False
    ai.close_hint = None
    session.add(_journal(ai.id, action="reopen"))
    if was_dismissed:
        # Как в restore: прежний образец говорил «этого обязательства нет», и поверх
        # него нужно записать исправленную правду.
        await _record_eval(session, ai.source_thread_id)
    return True


async def keep_open(session: AsyncSession, item_id: uuid.UUID) -> None:
    """«Оставить» в секции «Предлагаю закрыть»: обязательство живо, предложение снимаем."""
    ai = await get_item(session, item_id)
    if ai is None or not ai.close_candidate:
        return
    ai.close_candidate = False
    ai.close_hint = None
    session.add(_journal(ai.id, action="keep_open"))


async def confirm(session: AsyncSession, item_id: uuid.UUID) -> None:
    ai = await get_item(session, item_id)
    if ai:
        ai.user_edited = True
        ai.user_verdict = UserVerdict.CONFIRMED
        session.add(_journal(ai.id, action="confirm"))
        await _record_eval(session, ai.source_thread_id)


async def edit(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    type_: str | None = None,
    title: str | None = None,
    counterparty: str | None = None,
    due_date: date | None = None,
) -> None:
    ai = await get_item(session, item_id)
    if not ai:
        return
    if type_ is not None and type_ != ai.type:
        session.add(_journal(ai.id, action="retype", to=type_))
        ai.type = type_
        # A manual type change re-activates a snoozed/archived item so it shows up
        # in the new section.
        if ai.status in (ActionStatus.SNOOZED, ActionStatus.DONE, ActionStatus.DISMISSED):
            ai.status = ActionStatus.OPEN
            ai.next_check = None
    if title is not None:
        ai.title = title
    if counterparty is not None:
        ai.counterparty = counterparty
    ai.due_date = due_date
    ai.user_edited = True
    ai.user_verdict = UserVerdict.EDITED
    await _record_eval(session, ai.source_thread_id)


async def set_type(session: AsyncSession, item_id: uuid.UUID, new_type: str) -> None:
    """Change an action item's type (drag&drop between sections). Merges instead of
    duplicating when an item with the same (thread, type, counterparty) already exists."""
    ai = await get_item(session, item_id)
    if not ai:
        return
    # Calendar-sourced items are not retypeable: they have no thread/counterparty, so
    # the merge key below would be all-NULL and would match unrelated items. Nothing
    # creates them any more (the prep gate is gone), but the closed ones stay in the
    # archive and stay openable.
    if ai.type == ActionType.MEETING_PREP or ai.source_event_id is not None:
        return
    if new_type == ActionType.MEETING_PREP:
        return  # nothing may be retyped INTO a type no source produces
    # Moving an item into a (live) type section means "make it active here": a snoozed
    # or archived item must be revived, otherwise it stays hidden from the target view.
    _archived = (ActionStatus.SNOOZED, ActionStatus.DONE, ActionStatus.DISMISSED)
    if ai.type == new_type:
        # Already the target type — nothing to retype, but still revive if it was
        # snoozed/archived (e.g. dragging a snoozed NEED_MY_REPLY into "Нужен ответ").
        if ai.status in _archived:
            ai.status = ActionStatus.OPEN
            ai.next_check = None
            ai.user_edited = True
        return
    # Respect the unique key (source_thread_id, type, counterparty): if a sibling
    # already occupies the target type, merge into it rather than violating it. Only a
    # thread makes that key meaningful: `== None` renders as IS NULL, so without the
    # guard every thread-less item (meeting, Telegram, quick-add) of the target type
    # with an empty counterparty would be «the same» and get merged away.
    existing = None
    if ai.source_thread_id is not None:
        existing = (
            await session.execute(
                select(ActionItem).where(
                    ActionItem.source_thread_id == ai.source_thread_id,
                    ActionItem.type == new_type,
                    ActionItem.counterparty == ai.counterparty,
                    ActionItem.id != ai.id,
                )
            )
        ).scalar_one_or_none()
    if existing is not None:
        if existing.status in _archived:
            existing.status = ActionStatus.OPEN  # revive the target
            existing.next_check = None
        existing.user_edited = True
        await session.delete(ai)
        session.add(_journal(existing.id, action="retype_merge", to=new_type))
        return
    ai.type = new_type
    if ai.status in _archived:
        ai.status = ActionStatus.OPEN
        ai.next_check = None
    ai.user_edited = True
    ai.user_verdict = UserVerdict.EDITED
    session.add(_journal(ai.id, action="retype", to=new_type))
    await _record_eval(session, ai.source_thread_id)


async def set_read(session: AsyncSession, email_id: uuid.UUID, read: bool) -> None:
    e = await get_email(session, email_id)
    if e is not None:
        e.read_at = datetime.now(UTC) if read else None
        # Mirror the read flag to the IMAP server (\Seen), off the UI path: enqueue a
        # MarkSeen event the worker handles with retry/backoff. The handler reads the
        # committed read_at, so it converges to whatever the final local state is.
        from email_reader.enums import EventType
        from email_reader.queue import enqueue

        await enqueue(session, EventType.MARK_SEEN, {"email_id": str(email_id)})


async def unread_inbox_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count(Email.id)).where(
                Email.direction == Direction.INBOUND, Email.read_at.is_(None)
            )
        )
    ).scalar_one()


async def unread_inbox_by_account(session: AsyncSession) -> dict:
    """Unread inbound count per mailbox (by occurrence — matches the inbox listing).

    Keyed by account_id; absent accounts have no unread. Used for the per-mailbox
    sidebar badge so counts don't leak across mailboxes."""
    rows = await session.execute(
        select(EmailOccurrence.account_id, func.count(func.distinct(Email.id)))
        .join(Email, Email.id == EmailOccurrence.email_id)
        .where(Email.direction == Direction.INBOUND, Email.read_at.is_(None))
        .group_by(EmailOccurrence.account_id)
    )
    return {aid: n for aid, n in rows.all()}


async def create_task(
    session: AsyncSession,
    *,
    title: str,
    space_id: uuid.UUID,
    due: date | None = None,
    type_: str = ActionType.TASK,
    counterparty: str | None = None,
    source_kind: str = SourceKind.MANUAL,
    source_ref: str | None = None,
    reason: str | None = None,
    confirmed: bool = True,
) -> ActionItem:
    """An item that has no thread behind it: quick-add (confirmed — the user typed it) or
    the Telegram bot (unconfirmed — it waits in «Разобрать»). Owned by a space."""
    ai = ActionItem(
        source_kind=source_kind, type=type_, title=title.strip()[:300], counterparty=counterparty,
        due_date=due, status=ActionStatus.OPEN, space_id=space_id, source_ref=source_ref,
        reason=reason, user_edited=confirmed,
        user_verdict=UserVerdict.CONFIRMED if confirmed else None,
    )
    session.add(ai)
    await session.flush()
    await search_index.index_card(session, ai)
    session.add(_journal(ai.id, kind="user_action" if confirmed else "intake",
                        action="create", source=source_kind))
    return ai


async def create_item_from_email(
    session: AsyncSession, email_id: uuid.UUID, type_: str
) -> ActionItem | None:
    """Create an action item from a message (right-click / drag&drop a mail row)."""
    e = await get_email(session, email_id)
    if e is None:
        return None
    space_id = (
        await session.execute(select(Account.space_id).where(Account.id == e.account_id))
    ).scalar_one_or_none() if e.account_id else None
    if space_id is None:
        return None  # mail detached from a deleted mailbox has no space to put a task in
    if e.direction == Direction.INBOUND:
        counterparty = e.from_name or e.from_addr
    else:
        first = e.to_addrs[0] if e.to_addrs else None
        counterparty = (first.get("name") or first.get("email")) if first else None
    title = (e.subject or "").strip()
    if not title and e.body_text:
        title = e.body_text.strip().splitlines()[0][:80]
    title = title or "Без темы"
    # De-dupe on the unique key so we don't create a second identical item.
    existing = (
        await session.execute(
            select(ActionItem).where(
                ActionItem.source_thread_id == e.thread_id,
                ActionItem.type == type_,
                ActionItem.counterparty == counterparty,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status in (ActionStatus.DONE, ActionStatus.DISMISSED):
            existing.status = ActionStatus.OPEN
        return existing
    ai = ActionItem(
        source_kind=SourceKind.MAIL, space_id=space_id,
        type=type_, title=title, counterparty=counterparty,
        source_thread_id=e.thread_id, source_message_id=e.id,
        status=ActionStatus.OPEN, user_edited=True, user_verdict=UserVerdict.EDITED,
        reason="создано вручную из письма",
    )
    session.add(ai)
    await session.flush()
    session.add(_journal(ai.id, action="create_from_email", type=type_))
    return ai
