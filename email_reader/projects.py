"""Projects: prompt-based grouping of correspondence and meetings + export to a folder.

A Project has a natural-language prompt describing what belongs to it and a folder on disk
(explicit, or under app_settings['projects_root']). An LLM classifies each thread against
all enabled projects (honoring exclusions in the prompt); matching threads are recorded
(source='auto') and their emails + attachments are exported as Markdown into the folder.
Manual assignments (source='manual') are user overrides that the classifier never touches.

The folder's shape — which files the machine owns and which are the user's — lives in
project_folder.py.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import clock, project_folder, prompts
from email_reader.attachments import _safe_name, attachment_file, ensure_attachments
from email_reader.enums import (
    ACTIONABLE_TYPES,
    LIVE_STATUSES,
    ActionStatus,
    Direction,
    EventType,
)
from email_reader.models import (
    ActionItem,
    AgentDecision,
    Email,
    Meeting,
    Project,
    ProjectMeeting,
    ProjectThread,
    Thread,
)
from email_reader.queue import enqueue
from email_reader.search import index as search_index

# Auto-assign only above this confidence; weaker matches aren't filed automatically.
MATCH_THRESHOLD = 0.6


def _ensure_dir(path_str: str) -> Path:
    p = Path(path_str).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


# --- CRUD ---


ACTIVE = "active"
PROPOSED = "proposed"


async def list_projects(
    session: AsyncSession, space_id: uuid.UUID | None = None
) -> list[Project]:
    """Нити пространства, кроме предложенных агентом и ещё не подтверждённых."""
    q = select(Project).where(Project.status == ACTIVE).order_by(Project.created_at)
    if space_id is not None:
        q = q.where(Project.space_id == space_id)
    return list((await session.execute(q)).scalars().all())


async def list_proposed(session: AsyncSession, space_id: uuid.UUID) -> list[Project]:
    """Предложения агента, ждущие «Да» или «Нет» владельца (docs/feature-topics.md §9.1)."""
    q = (select(Project).where(Project.status == PROPOSED, Project.space_id == space_id)
         .order_by(Project.created_at.desc()))
    return list((await session.execute(q)).scalars().all())


async def list_enabled(
    session: AsyncSession, space_id: uuid.UUID | None = None
) -> list[Project]:
    q = (select(Project).where(Project.enabled.is_(True), Project.status == ACTIVE)
         .order_by(Project.created_at))
    if space_id is not None:
        q = q.where(Project.space_id == space_id)
    return list((await session.execute(q)).scalars().all())


async def get_project(session: AsyncSession, project_id: uuid.UUID) -> Project | None:
    return (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()


async def create_project(
    session: AsyncSession,
    *,
    name: str,
    folder_path: str = "",
    prompt: str,
    space_id: uuid.UUID,
    include_meetings: bool = False,
    aliases: list[str] | None = None,
) -> Project:
    # An empty folder means «under the shared projects root» — resolved at export time,
    # so the project keeps working if that root moves.
    folder = str(_ensure_dir(folder_path)) if folder_path.strip() else None
    project = Project(
        name=name.strip(), folder_path=folder, prompt=prompt.strip(),
        space_id=space_id, include_meetings=include_meetings,
        aliases=_clean_aliases(aliases),
    )
    session.add(project)
    await session.flush()
    await search_index.index_project(session, project)
    return project


def _clean_aliases(aliases: list[str] | None) -> list[str]:
    seen: list[str] = []
    for a in aliases or []:
        text = (a or "").strip()
        if text and text.lower() not in {s.lower() for s in seen}:
            seen.append(text)
    return seen


async def update_project(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    name: str | None = None,
    folder_path: str | None = None,
    prompt: str | None = None,
    enabled: bool | None = None,
    include_meetings: bool | None = None,
    aliases: list[str] | None = None,
) -> Project | None:
    p = await get_project(session, project_id)
    if p is None:
        return None
    if name is not None:
        p.name = name.strip()
    if folder_path is not None:
        p.folder_path = str(_ensure_dir(folder_path)) if folder_path.strip() else None
    if prompt is not None:
        p.prompt = prompt.strip()
    if enabled is not None:
        p.enabled = enabled
    if include_meetings is not None:
        p.include_meetings = include_meetings
    if aliases is not None:
        p.aliases = _clean_aliases(aliases)
    return p


async def delete_project(session: AsyncSession, project_id: uuid.UUID) -> None:
    await session.execute(delete(Project).where(Project.id == project_id))


# --- Membership ---


# Кто привязал: владелец рукой, агент через MCP, авторазбор. Старший не перебивается
# младшим; протухшие снимает только авторазбор у своих (`_remove_stale_auto`).
_RANK = {"manual": 2, "agent": 1, "auto": 0}


async def assign(
    session: AsyncSession,
    project_id: uuid.UUID,
    thread_id: uuid.UUID,
    *,
    source: str = "auto",
    confidence: float | None = None,
    reason: str | None = None,
) -> None:
    existing = (
        await session.execute(
            select(ProjectThread).where(
                ProjectThread.project_id == project_id,
                ProjectThread.thread_id == thread_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Старшинство manual > agent > auto: авторазбор не перебивает ни владельца, ни
        # агента; агент не перебивает владельца (docs/feature-topics.md §9.1 п. 3).
        if _RANK.get(source, 0) < _RANK.get(existing.source, 0):
            return
        existing.source = source
        existing.confidence = confidence
        existing.reason = reason
        return
    session.add(
        ProjectThread(
            project_id=project_id, thread_id=thread_id,
            source=source, confidence=confidence, reason=reason,
        )
    )


async def unassign(session: AsyncSession, project_id: uuid.UUID, thread_id: uuid.UUID) -> None:
    # Only drop the membership row. The project folder is additive — we never delete
    # exported files (removing a thread from the UI must not touch what's on disk). To
    # (re)build the folder from current memberships, use export_all_threads_for_project.
    await session.execute(
        delete(ProjectThread).where(
            ProjectThread.project_id == project_id,
            ProjectThread.thread_id == thread_id,
        )
    )


# --- Membership: meetings (sister helpers to the thread ones above; see §3.2 of
# docs/feature-routing.md — duplicated on purpose so the mail paths stay untouched) ---


async def list_for_meetings(
    session: AsyncSession, space_id: uuid.UUID | None = None
) -> list[Project]:
    """Candidate projects for a recorded meeting: enabled, opted in, and — when the
    meeting's space is known — of that space."""
    q = select(Project).where(
        Project.enabled.is_(True), Project.include_meetings.is_(True), Project.status == ACTIVE
    )
    if space_id is not None:
        q = q.where(Project.space_id == space_id)
    return list(
        (
            await session.execute(
                q.order_by(Project.created_at)
            )
        ).scalars().all()
    )


async def assign_meeting(
    session: AsyncSession,
    project_id: uuid.UUID,
    meeting_id: uuid.UUID,
    *,
    source: str = "auto",
    confidence: float | None = None,
    reason: str | None = None,
    digest: str | None = None,
) -> None:
    existing = (
        await session.execute(
            select(ProjectMeeting).where(
                ProjectMeeting.project_id == project_id,
                ProjectMeeting.meeting_id == meeting_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Never let auto-classification downgrade a manual assignment. A fresh digest is
        # still welcome: it describes the meeting, not the membership decision.
        if digest:
            existing.digest = digest
        if _RANK.get(source, 0) < _RANK.get(existing.source, 0):
            return
        existing.source = source
        existing.confidence = confidence
        existing.reason = reason
        return
    session.add(
        ProjectMeeting(
            project_id=project_id, meeting_id=meeting_id,
            source=source, confidence=confidence, reason=reason, digest=digest,
        )
    )


async def unassign_meeting(
    session: AsyncSession, project_id: uuid.UUID, meeting_id: uuid.UUID
) -> None:
    # Membership row only. The note already written into the project folder is marked
    # stale by the exporter, never deleted — removing files is the user's call.
    await session.execute(
        delete(ProjectMeeting).where(
            ProjectMeeting.project_id == project_id,
            ProjectMeeting.meeting_id == meeting_id,
        )
    )


async def projects_for_meeting(session: AsyncSession, meeting_id: uuid.UUID) -> list[Project]:
    return list(
        (
            await session.execute(
                select(Project)
                .join(ProjectMeeting, ProjectMeeting.project_id == Project.id)
                .where(ProjectMeeting.meeting_id == meeting_id)
                .order_by(Project.name)
            )
        ).scalars().all()
    )


async def meetings_for_project(session: AsyncSession, project_id: uuid.UUID) -> list:
    from email_reader.models import Meeting

    return list(
        (
            await session.execute(
                select(Meeting)
                .join(ProjectMeeting, ProjectMeeting.meeting_id == Meeting.id)
                .where(ProjectMeeting.project_id == project_id, Meeting.deleted_at.is_(None))
                .order_by(Meeting.started_at.desc())
            )
        ).scalars().all()
    )


async def meeting_memberships(
    session: AsyncSession, meeting_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ProjectMeeting]]:
    """meeting_id -> its membership rows (for rendering lists without N+1 queries)."""
    ids = [m for m in set(meeting_ids) if m]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(ProjectMeeting).where(ProjectMeeting.meeting_id.in_(ids))
        )
    ).scalars().all()
    out: dict[uuid.UUID, list[ProjectMeeting]] = {}
    for r in rows:
        out.setdefault(r.meeting_id, []).append(r)
    return out


async def remove_stale_meeting_auto(
    session: AsyncSession, meeting_id: uuid.UUID, keep: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Drop machine-made memberships the latest routing pass no longer confirms; manual
    ones are the user's word and stay. Returns the projects that lost the meeting, so the
    exporter can flag their notes."""
    rows = (
        await session.execute(
            select(ProjectMeeting).where(
                ProjectMeeting.meeting_id == meeting_id,
                ProjectMeeting.source != "manual",
            )
        )
    ).scalars().all()
    removed: set[uuid.UUID] = set()
    for r in rows:
        if r.project_id not in keep:
            removed.add(r.project_id)
            await session.delete(r)
    return removed


@dataclass
class Stalled:
    """Проект, по которому есть открытые обязательства, но давно ничего не происходило."""

    project_id: uuid.UUID
    name: str
    days: int
    open_cards: int


async def _max_by_project(session: AsyncSession, queries: list) -> dict[uuid.UUID, datetime]:
    out: dict[uuid.UUID, datetime] = {}
    for q in queries:
        for pid, at in await session.execute(q):
            if at is not None and (pid not in out or at > out[pid]):
                out[pid] = at
    return out


async def _unique_name(session: AsyncSession, space_id: uuid.UUID, base: str) -> str:
    """Имя Нити уникально в пространстве: второй «Договор» становится «Договор (2)»."""
    base = " ".join((base or "").split())[:80] or "Новая нить"
    taken = {
        n.casefold() for n in (await session.execute(
            select(Project.name).where(Project.space_id == space_id)
        )).scalars()
    }
    name, n = base, 2
    while name.casefold() in taken:
        name = f"{base} ({n})"
        n += 1
    return name


async def create_from_thread(
    session: AsyncSession, *, space_id: uuid.UUID, thread_id: uuid.UUID
) -> Project:
    """«Сделать нитью» из беседы: имя — тема последнего письма, беседа привязана
    рукой, встречи учитываются (docs/feature-topics.md §8.5)."""
    th = await session.get(Thread, thread_id)
    last = (await session.execute(
        select(Email).where(Email.thread_id == thread_id).order_by(Email.sent_at.desc()).limit(1)
    )).scalar_one_or_none()
    base = (last.subject if last and last.subject else (th.subject_norm if th else "")) or ""
    project = await create_project(
        session, name=await _unique_name(session, space_id, base), prompt="",
        space_id=space_id, include_meetings=True,
    )
    await assign(session, project.id, thread_id, source="manual", reason="new thread")
    return project


async def create_from_meeting(session: AsyncSession, meeting: Meeting) -> Project:
    """«Сделать нитью» из встречи: имя — её тема, встреча привязана рукой."""
    base = meeting.title or (
        f"Встреча {meeting.started_at:%d.%m}" if meeting.started_at else "Встреча"
    )
    project = await create_project(
        session, name=await _unique_name(session, meeting.space_id, base), prompt="",
        space_id=meeting.space_id, include_meetings=True,
    )
    await assign_meeting(session, project.id, meeting.id, source="manual", reason="new thread")
    return project


async def create_from_card(session: AsyncSession, item: ActionItem) -> Project:
    """«Сделать нитью» из обязательства: имя — его заголовок, карточка привязана прямо."""
    project = await create_project(
        session, name=await _unique_name(session, item.space_id, item.title), prompt="",
        space_id=item.space_id, include_meetings=True,
    )
    item.project_id = project.id
    return project


EVIDENCE_KINDS = ("conversation", "meeting", "card")


async def resolve_evidence(session: AsyncSession, ids: list[str]) -> list[dict]:
    """Улики предложения: id бесед, встреч и обязательств → {kind, id, title}. Что не
    нашлось — отбрасывается; пустой список значит, что улик нет."""
    out: list[dict] = []
    for raw in ids or []:
        try:
            ref = uuid.UUID(str(raw).strip())
        except ValueError:
            continue
        th = await session.get(Thread, ref)
        if th is not None:
            last = (await session.execute(
                select(Email).where(Email.thread_id == ref).order_by(Email.sent_at.desc()).limit(1)
            )).scalar_one_or_none()
            out.append({"kind": "conversation", "id": str(ref),
                        "title": (last.subject if last else th.subject_norm) or "беседа"})
            continue
        m = await session.get(Meeting, ref)
        if m is not None and m.deleted_at is None:
            out.append({"kind": "meeting", "id": str(ref), "title": m.title or "встреча"})
            continue
        it = await session.get(ActionItem, ref)
        if it is not None:
            out.append({"kind": "card", "id": str(ref), "title": it.title})
    return out


async def propose(
    session: AsyncSession, *, space_id: uuid.UUID, title: str, context: str = "",
    aliases: list[str] | None = None, evidence: list[dict], why: str, by: str = "",
) -> Project:
    """Предложение Нити от агента (docs/feature-topics.md §9.1 п. 1): статус `proposed`,
    до «Да» владельца — ни в маршрутизации, ни в списках, ни в поиске."""
    project = Project(
        name=await _unique_name(session, space_id, title), prompt=(context or "").strip(),
        space_id=space_id, include_meetings=True, aliases=_clean_aliases(aliases),
        status=PROPOSED, proposal={"why": (why or "").strip(), "evidence": evidence, "by": by},
    )
    session.add(project)
    await session.flush()
    session.add(AgentDecision(kind="topic_candidate", payload={
        "project": str(project.id), "name": project.name, "by": by, "verdict": None,
        "evidence": [e["id"] for e in evidence],
    }))
    return project


async def accept(session: AsyncSession, project: Project) -> None:
    """«Да»: Нить становится обычной, улики предложения привязываются как `agent`."""
    project.status = ACTIVE
    for e in (project.proposal or {}).get("evidence", []):
        ref = uuid.UUID(e["id"])
        if e["kind"] == "conversation":
            await assign(session, project.id, ref, source="agent", reason="proposal")
        elif e["kind"] == "meeting":
            await assign_meeting(session, project.id, ref, source="agent", reason="proposal")
        elif e["kind"] == "card":
            it = await session.get(ActionItem, ref)
            if it is not None and it.project_id is None:
                it.project_id = project.id
    await search_index.index_project(session, project)
    session.add(AgentDecision(kind="topic_candidate", payload={
        "project": str(project.id), "name": project.name, "verdict": "yes",
        "evidence": [e["id"] for e in (project.proposal or {}).get("evidence", [])],
    }))


async def reject(session: AsyncSession, project: Project) -> None:
    """«Нет»: предложение снимается, а его улики запоминаются, чтобы сигнал по ним не
    всплывал снова (`list_thread_signals`)."""
    session.add(AgentDecision(kind="topic_candidate", payload={
        "project": str(project.id), "name": project.name, "verdict": "no",
        "evidence": [e["id"] for e in (project.proposal or {}).get("evidence", [])],
    }))
    await delete_project(session, project.id)


async def rejected_sources(session: AsyncSession) -> set[str]:
    """Id бесед, встреч и карточек, по которым владелец уже сказал «Нет»."""
    rows = (await session.execute(
        select(AgentDecision.payload).where(AgentDecision.kind == "topic_candidate")
    )).scalars().all()
    out: set[str] = set()
    for payload in rows:
        if (payload or {}).get("verdict") == "no":
            out.update(str(x) for x in payload.get("evidence", []))
    return out


def card_member(project_id: uuid.UUID):  # noqa: ANN201 — SQLAlchemy predicate
    """Предикат «карточка принадлежит Нити»: прямая привязка, если стоит, иначе через
    беседу или встречу (docs/feature-topics.md §8.3). Одно правило на всех читателей —
    досье, брифинг, MCP, группировку доски; расходятся они только здесь."""
    tids = select(ProjectThread.thread_id).where(ProjectThread.project_id == project_id)
    mids = select(ProjectMeeting.meeting_id).where(ProjectMeeting.project_id == project_id)
    return or_(
        ActionItem.project_id == project_id,
        and_(
            ActionItem.project_id.is_(None),
            or_(ActionItem.source_thread_id.in_(tids), ActionItem.source_meeting_id.in_(mids)),
        ),
    )


async def stalled(
    session: AsyncSession, space_id: uuid.UUID, days: int, now: datetime | None = None
) -> list[Stalled]:
    """Проекты пространства, которые замерли: открытые обязательства есть, а движения —
    ни письма в тредах проекта, ни встречи, ни правки карточки — дольше `days`.

    Считается запросами, без модели: это наблюдение о данных, а не вывод
    (docs/feature-card-advice.md §1.8). Проект без открытых карточек не «замер», он просто
    тихий, и сюда не попадает."""
    now = now or datetime.now(UTC)
    rows = (await session.execute(
        select(Project).where(Project.space_id == space_id, Project.enabled.is_(True))
    )).scalars().all()
    ids = [p.id for p in rows]
    if not ids:
        return []
    open_states = [*(s.value for s in LIVE_STATUSES), ActionStatus.SNOOZED.value]
    live = [
        ActionItem.status.in_(open_states),
        ActionItem.type.in_([t.value for t in ACTIONABLE_TYPES]),
    ]
    last = await _max_by_project(session, [
        select(ProjectThread.project_id, func.max(Email.sent_at))
        .join(Email, Email.thread_id == ProjectThread.thread_id)
        .where(ProjectThread.project_id.in_(ids)).group_by(ProjectThread.project_id),
        select(ProjectMeeting.project_id, func.max(Meeting.started_at))
        .join(Meeting, Meeting.id == ProjectMeeting.meeting_id)
        .where(ProjectMeeting.project_id.in_(ids), Meeting.deleted_at.is_(None))
        .group_by(ProjectMeeting.project_id),
        # Правка карточки — тоже движение: я что-то с этим делал. Карточка с прямой
        # привязкой считается только у своей Нити.
        select(ProjectThread.project_id, func.max(ActionItem.updated_at))
        .join(ActionItem, ActionItem.source_thread_id == ProjectThread.thread_id)
        .where(ProjectThread.project_id.in_(ids), ActionItem.project_id.is_(None))
        .group_by(ProjectThread.project_id),
        select(ProjectMeeting.project_id, func.max(ActionItem.updated_at))
        .join(ActionItem, ActionItem.source_meeting_id == ProjectMeeting.meeting_id)
        .where(ProjectMeeting.project_id.in_(ids), ActionItem.project_id.is_(None))
        .group_by(ProjectMeeting.project_id),
        select(ActionItem.project_id, func.max(ActionItem.updated_at))
        .where(ActionItem.project_id.in_(ids)).group_by(ActionItem.project_id),
    ])
    counts: dict[uuid.UUID, int] = {}
    for q in (
        select(ProjectThread.project_id, func.count(ActionItem.id))
        .join(ActionItem, ActionItem.source_thread_id == ProjectThread.thread_id)
        .where(ProjectThread.project_id.in_(ids), ActionItem.project_id.is_(None), *live)
        .group_by(ProjectThread.project_id),
        select(ProjectMeeting.project_id, func.count(ActionItem.id))
        .join(ActionItem, ActionItem.source_meeting_id == ProjectMeeting.meeting_id)
        .where(ProjectMeeting.project_id.in_(ids), ActionItem.project_id.is_(None), *live)
        .group_by(ProjectMeeting.project_id),
        select(ActionItem.project_id, func.count(ActionItem.id))
        .where(ActionItem.project_id.in_(ids), *live).group_by(ActionItem.project_id),
    ):
        for pid, n in await session.execute(q):
            counts[pid] = counts.get(pid, 0) + (n or 0)
    out: list[Stalled] = []
    for p in rows:
        at, open_cards = last.get(p.id), counts.get(p.id, 0)
        if at is None or not open_cards:
            continue
        idle = (now - at).days
        if idle >= days:
            out.append(Stalled(project_id=p.id, name=p.name, days=idle, open_cards=open_cards))
    return sorted(out, key=lambda s: s.days, reverse=True)


async def projects_for_thread(session: AsyncSession, thread_id: uuid.UUID) -> list[Project]:
    return list(
        (
            await session.execute(
                select(Project)
                .join(ProjectThread, ProjectThread.project_id == Project.id)
                .where(ProjectThread.thread_id == thread_id)
                .order_by(Project.name)
            )
        ).scalars().all()
    )


async def _thread_emails(session: AsyncSession, thread_id: uuid.UUID) -> list[Email]:
    return list(
        (
            await session.execute(
                select(Email).where(Email.thread_id == thread_id).order_by(Email.sent_at)
            )
        ).scalars().all()
    )


def _render_messages(emails: list[Email]) -> list[dict]:
    out = []
    for e in emails:
        out.append(
            {
                "is_me": e.direction in (Direction.OUTBOUND, Direction.SELF),
                "who": e.from_name or e.from_addr,
                "sent_at": e.sent_at,
                "subject": e.subject,
                "body": e.body_text,
            }
        )
    return out


# --- Classification handler (EventType.PROJECT_MATCH) ---


async def process_project_match(session: AsyncSession, payload: dict) -> None:
    from email_reader import spaces as spaces_svc
    from email_reader.model_connections import resolve_for_space

    thread_id = uuid.UUID(payload["thread_id"])
    thread = (
        await session.execute(select(Thread).where(Thread.id == thread_id))
    ).scalar_one_or_none()
    if thread is None:
        return
    # Only this space's projects, classified by this space's model (isolation).
    space_id = await spaces_svc.space_of_account(session, thread.account_id)
    if space_id is None:
        return
    projects = await list_enabled(session, space_id)
    if not projects:
        return
    rm = await resolve_for_space(session, space_id)
    if not rm.api_key:
        return  # no usable model -> skip quietly
    emails = await _thread_emails(session, thread_id)
    if not emails:
        return

    from email_reader import prompts
    from email_reader.ai.extractor import classify_projects
    system_prompt = await prompts.get_project_prompt(session, space_id)
    result, usage = await classify_projects(
        _render_messages(emails),
        clock.today(),
        [{"name": p.name, "prompt": p.prompt} for p in projects],
        kind=rm.kind, base_url=rm.base_url, api_key=rm.api_key, model=rm.model,
        no_thinking=rm.no_thinking,
        system_prompt=system_prompt,
    )

    by_name = {p.name: p for p in projects}
    matched: set[uuid.UUID] = set()
    for m in result.matches:
        p = by_name.get(m.project)
        if p and m.member and m.confidence >= MATCH_THRESHOLD:
            await assign(
                session, p.id, thread_id,
                source="auto", confidence=m.confidence, reason=m.reason,
            )
            matched.add(p.id)

    # Drop auto rows for projects no longer matched (keep manual overrides).
    await _remove_stale_auto(session, thread_id, keep=matched)

    session.add(
        AgentDecision(
            kind="project_match",
            payload={
                "thread_id": str(thread_id),
                "matched": [str(i) for i in matched],
            },
            model=rm.model,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
        )
    )

    # Export every current membership (auto + manual) to its folder.
    for p in await projects_for_thread(session, thread_id):
        try:
            await export_thread_to_project(session, p, thread, emails)
        except Exception:  # noqa: BLE001 — export must not fail the whole event
            pass


async def _remove_stale_auto(
    session: AsyncSession, thread_id: uuid.UUID, keep: set[uuid.UUID]
) -> None:
    rows = (
        await session.execute(
            select(ProjectThread).where(
                ProjectThread.thread_id == thread_id, ProjectThread.source == "auto"
            )
        )
    ).scalars().all()
    for r in rows:
        if r.project_id not in keep:
            await session.delete(r)


log = logging.getLogger("email_reader.projects")


# --- Export ---


def _thread_slug(thread: Thread, emails: list[Email]) -> str:
    subject = (emails[-1].subject if emails else None) or "(без темы)"
    return f"{_safe_name(subject)[:60]}-{str(thread.id)[:8]}"


# --- Thread digest (optional; off unless the mailbox has a «Конспект переписки» prompt) ---

# Enough of a long thread for a digest without paying for the whole archive.
_DIGEST_INPUT_CHARS = 24_000
SUMMARY_FILE = "Сводка.md"


def _thread_watermark(emails: list[Email]) -> str:
    """What the digest was made from. Stored in the file so a re-export of an unchanged
    thread costs nothing — /project/{id}/export re-runs EVERY thread on one click."""
    last = emails[-1]
    return f"{len(emails)}:{last.id}"


def _watermark_of(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines()[:8]:
        if line.startswith("source: "):
            return line[len("source: "):].strip()
    return None


def _thread_as_text(emails: list[Email]) -> str:
    parts = []
    for e in emails:
        who = e.from_name or e.from_addr
        when = e.sent_at.strftime("%Y-%m-%d %H:%M") if e.sent_at else "?"
        parts.append(f"[{when}] {who}: {e.body_text or ''}".strip())
    return "\n\n".join(parts)[:_DIGEST_INPUT_CHARS]


async def _thread_digest(
    session: AsyncSession, project: Project, emails: list[Email], system: str
) -> str | None:
    """The digest text, or None if it could not be produced.

    Best-effort by construction: export runs inside UI actions and the worker, so a model
    outage must degrade to copying the letters, never fail filing the thread."""
    from email_reader import model_connections as mc
    from email_reader.ai.extractor import complete_text

    try:
        rm = await mc.resolve_for_space(session, project.space_id)
        if rm is None:
            return None
        return await complete_text(
            kind=rm.kind, base_url=rm.base_url, api_key=rm.api_key, model=rm.model,
            no_thinking=rm.no_thinking,
            system=system, user=_thread_as_text(emails), max_tokens=900,
        )
    except Exception:  # noqa: BLE001 — see the docstring
        log.warning("Digest of thread %s failed; copying the letters instead",
                    emails[0].thread_id, exc_info=True)
        return None


async def export_thread_to_project(
    session: AsyncSession, project: Project, thread: Thread, emails: list[Email] | None = None
) -> None:
    """Write a thread's emails (Markdown) + attachments into the project folder. Idempotent.

    Threads now live under «Переписка/» instead of the project root, which the meeting
    notes and summaries share. Folders exported by earlier versions stay where they are —
    the exporter only ever adds (invariant 1)."""
    if emails is None:
        emails = await _thread_emails(session, thread.id)
    if not emails:
        return
    root = await project_folder.project_dir(session, project, create=True)
    if root is None:
        return  # no folder configured for this project yet — nothing to export into
    base = root / project_folder.MAIL_DIR / _thread_slug(thread, emails)
    base.mkdir(parents=True, exist_ok=True)

    # A digest REPLACES the per-letter files (that is what «иначе копируется как есть»
    # means). Attachments are copied either way — they are the part a digest cannot carry.
    system = await prompts.get_mail_summary_prompt(session, project.space_id)
    digest = None
    if system:
        mark = _thread_watermark(emails)
        summary_path = base / SUMMARY_FILE
        if _watermark_of(summary_path) == mark:
            digest = ""            # unchanged since the last export — no model call
        else:
            digest = await _thread_digest(session, project, emails, system)
            if digest:
                project_folder.write_auto(
                    summary_path, emails[-1].subject or "(без темы)",
                    f"source: {mark}\n\n{digest}",
                )
    # Letters exported before the prompt was set are NOT deleted (invariant «только
    # добавляем») — such a folder simply ends up holding both.
    for i, e in enumerate(emails, 1):
        if digest is None:
            who = e.from_name or e.from_addr
            to = ", ".join(a.get("email", "") for a in (e.to_addrs or []))
            when = e.sent_at.strftime("%Y-%m-%d %H:%M") if e.sent_at else "?"
            md = (
                f"# {e.subject or '(без темы)'}\n\n"
                f"- **От:** {who} <{e.from_addr}>\n"
                f"- **Кому:** {to}\n"
                f"- **Дата:** {when}\n\n---\n\n"
                f"{e.body_text or '(пустое письмо)'}\n"
            )
            (base / f"{i:02d}-{_safe_name(who)[:30]}.md").write_text(md, encoding="utf-8")

        # Attachments: reuse the on-demand cache, copy bytes next to the message.
        meta = await ensure_attachments(session, e.id)
        for idx, att in enumerate(meta):
            found = attachment_file(e.id, idx)
            if found is None:
                continue
            src, _m = found
            (base / f"{i:02d}-att-{att['name']}").write_bytes(src.read_bytes())

    project_folder.write_readme(project, root)
    await project_folder.write_summaries(session, project, root)


async def export_meeting(
    session: AsyncSession, meeting, *, removed: set[uuid.UUID] = frozenset()
) -> dict:
    """Write the meeting's note into every project it belongs to and refresh their
    summaries; flag the note in folders that just lost it.

    One recording, three projects → three different texts: the front matter and the link
    to the archive are shared, the body is what the meeting means for that project."""
    from email_reader import meetings as meetings_svc

    stats = {"notes": 0, "stale": 0, "blocked": 0}
    items = list(
        (
            await session.execute(
                select(ActionItem).where(ActionItem.source_meeting_id == meeting.id)
            )
        ).scalars().all()
    )
    rows = list(
        (
            await session.execute(
                select(ProjectMeeting).where(ProjectMeeting.meeting_id == meeting.id)
            )
        ).scalars().all()
    )
    by_id = {p.id: p for p in await list_projects(session)}
    names = [by_id[r.project_id].name for r in rows if r.project_id in by_id]

    links: dict[str, str] = {}
    transcript = await meetings_svc.resolve(session, meeting.transcript_path)
    if transcript is not None:
        links["Полный транскрипт"] = transcript.as_uri()
    audio = await meetings_svc.resolve(session, meeting.audio_path)
    if audio is not None:
        links["Аудио"] = audio.as_uri()

    for row in rows:
        project = by_id.get(row.project_id)
        if project is None:
            continue
        try:
            base = await project_folder.project_dir(session, project, create=True)
            if base is None:
                continue
            note = base / project_folder.MEETINGS_DIR / project_folder.note_name(meeting)
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(
                project_folder.render_meeting_note(
                    meeting, project, row, items, names, row.digest, links
                ),
                encoding="utf-8",
            )
            project_folder.write_readme(project, base)
            await project_folder.write_summaries(session, project, base)
            stats["notes"] += 1
        except project_folder.ExportBlocked:
            stats["blocked"] += 1
        except OSError:
            stats["blocked"] += 1

    for pid in removed:
        project = by_id.get(pid)
        if project is None:
            continue
        try:
            base = await project_folder.project_dir(session, project)
            if base is None:
                continue
            # Note names carry the meeting title; a renamed meeting leaves the old file
            # untouched, which is the safe direction — we never delete.
            path = base / project_folder.MEETINGS_DIR / project_folder.note_name(meeting)
            if project_folder.mark_note_stale(path):
                stats["stale"] += 1
            await project_folder.write_summaries(session, project, base)
        except (project_folder.ExportBlocked, OSError):
            stats["blocked"] += 1
    return stats


async def export_all_threads_for_project(session: AsyncSession, project: Project) -> int:
    """Export every thread currently assigned to the project. Additive — writes/refreshes
    files but never deletes anything already in the folder. Returns count exported."""
    root = await project_folder.project_dir(session, project, create=True)
    if root is None:
        raise project_folder.ExportBlocked(
            "У проекта не задана папка, и общий корень проектов не настроен."
        )
    threads = (
        await session.execute(
            select(Thread)
            .join(ProjectThread, ProjectThread.thread_id == Thread.id)
            .where(ProjectThread.project_id == project.id)
            .order_by(Thread.last_at)
        )
    ).scalars().all()
    n = 0
    for thread in threads:
        try:
            await export_thread_to_project(session, project, thread)
            n += 1
        except Exception:  # noqa: BLE001 — one bad thread must not abort the export
            pass
    project_folder.write_readme(project, root)
    await project_folder.write_summaries(session, project, root)
    return n


# --- Backfill ---


async def enqueue_backfill(session: AsyncSession, days: int = 90) -> int:
    """Queue project classification for threads active within the last `days`."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await session.execute(select(Thread.id).where(Thread.last_at >= cutoff))
    ).all()
    for (tid,) in rows:
        await enqueue(session, EventType.PROJECT_MATCH, {"thread_id": str(tid)})
    return len(rows)
