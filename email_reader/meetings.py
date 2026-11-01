"""Meetings: intake of transcripts produced by the recorder app.

The handover contract is a **folder**, not HTTP: the desktop backend picks its port at
startup, so the producer cannot address it. the recorder drops one json per meeting into
``<data dir>/artifacts_inbox``; a periodic sweep indexes it as a ``Meeting``, queues a
``MeetingReady`` event and deletes the json.

Files in the ASR archive are only ever **read**. A ``Meeting`` is an index over them:
paths are stored relative to the archive root (``app_settings['meetings.asr_root']``, kept
in sync from the payload) so moving the archive relinks everything by editing one setting.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import clock, settings_store
from email_reader import spaces as spaces_svc
from email_reader.db import session_scope
from email_reader.enums import (
    ACTIONABLE_TYPES,
    LIVE_STATUSES,
    ActionStatus,
    ActionType,
    EventStatus,
    EventType,
    SourceKind,
)
from email_reader.models import ActionItem, AgentDecision, Meeting, Project, ProjectMeeting
from email_reader.paths import get_data_dir
from email_reader.queue import enqueue
from email_reader.search import index as search_index

log = logging.getLogger("email_reader.meetings")

INBOX_DIRNAME = "artifacts_inbox"
ASR_ROOT_KEY = "meetings.asr_root"
# Space that new meetings land in (docs/spaces.md §3): their tasks show on its boards,
# its prompt override and profile are the ones used. A meeting can be moved afterwards.
SPACE_KEY = "meetings.space_id"
# Model that reads meetings, chosen among the connected ones. Empty = the space's model.
MODEL_KEY = "meetings.model_id"

# Confidence zones (§7 of docs/feature-routing.md). Deliberately above mail's 0.6: with no
# calendar signal the classifier's weak side is precision, and a meeting filed into the
# wrong project is far more visible than a misfiled email. The cap bounds the damage of a
# model that says yes to everything.
AUTO_THRESHOLD = 0.8
SUGGEST_THRESHOLD = 0.5
MAX_AUTO_PROJECTS = 3

# A json that fails to parse is left alone until it is this old: an atomic producer never
# exposes a half-written file, but a non-atomic one would, and quarantining an in-flight
# write would lose the meeting.
_QUARANTINE_AFTER = timedelta(minutes=5)

# Transcript bytes read for the title (first meaningful line) and for the reading pane.
_TITLE_HEAD_BYTES = 4096
_TRANSCRIPT_MAX_CHARS = 200_000

_TITLE_MAX_CHARS = 90
# How much of the transcript's opening feeds the memory lookup: the title plus the
# first minutes say what the meeting is about; the rest is the model's to read.
_MEMORY_QUERY_CHARS = 600


def inbox_dir() -> Path:
    d = get_data_dir() / INBOX_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _quarantine_dir() -> Path:
    d = inbox_dir() / "rejected"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _quarantine(path: Path, why: str) -> None:
    """Move an unusable payload aside instead of deleting it — a dropped meeting must
    stay visible somewhere (invariant «ничего не пропадает молча»)."""
    try:
        path.rename(_quarantine_dir() / path.name)
    except OSError:
        log.warning("Could not quarantine %s (%s)", path.name, why, exc_info=True)
    else:
        log.warning("Rejected artifact %s: %s", path.name, why)


def _parse_started(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    # Producer and router run on the same machine, so a timestamp without an offset is
    # local wall-clock — astimezone() attaches the local zone; reading it as UTC would
    # shift every meeting by the UTC offset.
    return dt.astimezone(UTC)


def _clean_path(value) -> str | None:
    if not isinstance(value, str):
        return None
    p = value.strip()
    return p or None


def _duration(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


# --- Path resolution ---


def locate(root: str | None, rel: str | None) -> Path | None:
    """Absolute path of an archived artifact, or None when it can't be located.

    Absolute stored paths are used as-is (a producer that ignores the convention still
    works); relative ones are joined to the archive root and must stay inside it."""
    if not rel:
        return None
    p = Path(rel).expanduser()
    if p.is_absolute():
        return p if p.exists() else None
    if not root:
        return None
    base = Path(root).expanduser().resolve()
    full = (base / p).resolve()
    if not full.is_relative_to(base):
        log.warning("Artifact path %r escapes the ASR root — ignored", rel)
        return None
    return full if full.exists() else None


async def get_asr_root(session: AsyncSession) -> str | None:
    return await settings_store.get_value(session, ASR_ROOT_KEY)


async def routing_space_id(session: AsyncSession) -> uuid.UUID:
    """The space new meetings land in: the one chosen on the «Встречи» screen, else the
    default space. A setting pointing at a deleted space falls back the same way instead
    of silently stopping meeting intake. There is always a space (spaces.ensure_default)."""
    raw = await settings_store.get_value(session, SPACE_KEY)
    return (await spaces_svc.resolve(session, raw)).id


async def routing_model(session: AsyncSession, space_id: uuid.UUID | None = None):
    """The model that reads meetings: the one picked on the «Встречи» screen, else the
    model of the space the meeting belongs to (the default meetings space when the
    meeting is not known yet).

    A setting pointing at a connection that was deleted, switched off, or whose key no
    longer decrypts falls back the same way — a stale pointer must not silently stop
    meetings from being read at all."""
    from email_reader.model_connections import (
        get_connection,
        resolve_connection,
        resolve_for_space,
    )

    raw = await settings_store.get_value(session, MODEL_KEY)
    if raw:
        try:
            chosen = uuid.UUID(raw)
        except ValueError:
            log.warning("Setting %s is not a uuid: %r", MODEL_KEY, raw)
        else:
            rm = resolve_connection(await get_connection(session, chosen))
            if rm is not None:
                return rm
    if space_id is None:
        space_id = await routing_space_id(session)
    return await resolve_for_space(session, space_id)


async def set_space(session: AsyncSession, meeting_id: uuid.UUID, space_id: uuid.UUID) -> bool:
    """Move a meeting — and everything extracted from it — to another space. Projects
    are per space, so memberships in and suggestions of the old space's projects are
    dropped; the summary and the items travel. No second model call: the user files
    the meeting under a project of the new space by hand from the pane."""
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None or await spaces_svc.get_space(session, space_id) is None:
        return False
    if meeting.space_id == space_id:
        return True
    meeting.space_id = space_id
    for ai in (
        await session.execute(
            select(ActionItem).where(ActionItem.source_meeting_id == meeting_id)
        )
    ).scalars().all():
        ai.space_id = space_id
    await session.execute(
        delete(ProjectMeeting).where(
            ProjectMeeting.meeting_id == meeting_id,
            ProjectMeeting.project_id.in_(select(Project.id).where(Project.space_id != space_id)),
        )
    )
    meeting.suggestions = []
    await session.flush()
    return True


# resolved_by marker on items closed by deleting their meeting: restore reopens exactly
# these and nothing the user closed himself.
MEETING_DELETED = "meeting_deleted"


async def delete_meeting(session: AsyncSession, meeting_id: uuid.UUID) -> Meeting | None:
    """Soft-delete: the meeting leaves lists, counts and routing; its machine-made live
    items are resolved. User-touched items (edited, ticked off) are left as they are —
    the same rule _sync_items follows. The row stays for ext_id dedup and for undo."""
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None:
        return None
    if meeting.deleted_at is None:
        meeting.deleted_at = datetime.now(UTC)
        for ai in (
            await session.execute(
                select(ActionItem).where(
                    ActionItem.source_meeting_id == meeting_id,
                    ActionItem.status.in_([s.value for s in LIVE_STATUSES]),
                    ActionItem.user_edited.is_(False),
                )
            )
        ).scalars().all():
            ai.status = ActionStatus.DONE
            ai.resolved_by = MEETING_DELETED
        await session.flush()
    return meeting


async def restore_meeting(session: AsyncSession, meeting_id: uuid.UUID) -> Meeting | None:
    """Undo of delete_meeting: the meeting is back, and so are the items it closed."""
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None:
        return None
    if meeting.deleted_at is not None:
        meeting.deleted_at = None
        for ai in (
            await session.execute(
                select(ActionItem).where(
                    ActionItem.source_meeting_id == meeting_id,
                    ActionItem.resolved_by == MEETING_DELETED,
                )
            )
        ).scalars().all():
            ai.status = ActionStatus.OPEN
            ai.resolved_by = None
        await session.flush()
    return meeting


async def resolve(session: AsyncSession, rel: str | None) -> Path | None:
    return locate(await get_asr_root(session), rel)


def _read(path: Path, max_bytes: int | None = None) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return fh.read() if max_bytes is None else fh.read(max_bytes)


# Every speaker turn in a transcript starts with one of these, and the model quotes it
# when it says where in the meeting something was said.
STAMP_RE = re.compile(r"\[(\d{1,2}):([0-5]\d):([0-5]\d)\]")


def quoted_timecode(reason: str | None) -> str | None:
    """The moment an item refers to, «[00:08:23]», or None."""
    m = STAMP_RE.search(reason or "")
    return m.group(0) if m else None


def stamp_seconds(stamp: str) -> int:
    """«[00:08:23]» -> 503. Zero when the string carries no timecode."""
    m = STAMP_RE.search(stamp)
    if not m:
        return 0
    h, mi, sec = (int(g) for g in m.groups())
    return h * 3600 + mi * 60 + sec


def timecode_span(transcript: str, at: str | None) -> tuple[int, int] | None:
    """Where that moment sits in the transcript: from the last stamp at or before it to
    the next one.

    Not a plain search for the stamp: the model quotes a moment it heard, and that exact
    second is usually not a stamp in the file. The turn that was running is what a reader
    opening the transcript needs to see."""
    if not transcript or not at or not STAMP_RE.search(at):
        return None
    want = stamp_seconds(at)
    start: int | None = None
    for m in STAMP_RE.finditer(transcript):
        if stamp_seconds(m.group(0)) <= want:
            start = m.start()
        elif start is not None:
            return start, m.start()
        else:
            return None
    return (start, len(transcript)) if start is not None else None


async def read_transcript(
    session: AsyncSession, meeting: Meeting, max_chars: int = _TRANSCRIPT_MAX_CHARS
) -> str | None:
    """Transcript text for display/summarization; None when the file is unreachable."""
    path = await resolve(session, meeting.transcript_path)
    if path is None:
        return None
    try:
        return _read(path)[:max_chars]
    except OSError:
        log.warning("Cannot read transcript %s", path, exc_info=True)
        return None


# --- Intake ---


async def ingest_payload(session: AsyncSession, payload: dict) -> Meeting | None:
    """Index one handover payload. Idempotent on ext_id: a re-delivered meeting updates
    the existing row (paths may have moved) instead of creating a second one."""
    ext_id = str(payload.get("meeting_id") or "").strip()
    started_at = _parse_started(payload.get("started_at"))
    transcript_path = _clean_path(payload.get("transcript_path"))
    if not ext_id or started_at is None or transcript_path is None:
        return None

    root = _clean_path(payload.get("asr_root"))
    if root:
        # The archive root is a property of the machine, not of one meeting: keep the
        # setting current so paths stored earlier resolve after the archive moves.
        await settings_store.set_value(session, ASR_ROOT_KEY, root)

    meeting = (
        await session.execute(select(Meeting).where(Meeting.ext_id == ext_id))
    ).scalar_one_or_none()
    if meeting is None:
        meeting = Meeting(
            ext_id=ext_id, started_at=started_at, space_id=await routing_space_id(session)
        )
        session.add(meeting)
    elif meeting.deleted_at is not None:
        # The user deleted this recording; a re-delivery must not bring it back.
        log.info("Ignoring re-delivered meeting %s: deleted by the user", ext_id)
        return meeting
    else:
        meeting.started_at = started_at
    meeting.duration_sec = _duration(payload.get("duration_sec"))
    meeting.transcript_path = transcript_path
    meeting.srt_path = _clean_path(payload.get("srt_path"))
    meeting.audio_path = _clean_path(payload.get("audio_path"))
    meeting.source = _clean_path(payload.get("source"))
    await session.flush()
    await search_index.index_meeting(session, meeting)
    # Re-delivery re-runs the handler on purpose: the transcript may have been redone.
    await enqueue(session, EventType.MEETING_READY, {"meeting_id": str(meeting.id)})
    return meeting


async def scan_inbox() -> dict:
    """Sweep the handover folder. One meeting per transaction; a bad payload never
    blocks the rest."""
    stats = {"ingested": 0, "rejected": 0}
    now = datetime.now(UTC)
    for path in sorted(inbox_dir().glob("*.json")):
        try:
            raw = _read(path)
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            except OSError:
                continue
            if now - mtime > _QUARANTINE_AFTER:
                _quarantine(path, f"unreadable: {exc}")
                stats["rejected"] += 1
            continue
        if not isinstance(payload, dict):
            _quarantine(path, "payload is not an object")
            stats["rejected"] += 1
            continue
        try:
            async with session_scope() as s:
                meeting = await ingest_payload(s, payload)
                ok = meeting is not None
        except Exception:  # noqa: BLE001 — one bad payload must not abort the sweep
            log.exception("Failed to ingest %s", path.name)
            continue
        if not ok:
            _quarantine(path, "missing meeting_id / started_at / transcript_path")
            stats["rejected"] += 1
            continue
        stats["ingested"] += 1
        # Delete only after the meeting and its event are committed. A failed unlink is
        # harmless — the next sweep re-ingests the same ext_id onto the same row.
        try:
            path.unlink()
        except OSError:
            log.warning("Ingested %s but could not remove it from the inbox", path.name)
    return stats


# --- Handler (EventType.MEETING_READY) ---


_SPEAKER_RE = re.compile(r"^(?:спикер|speaker|участник)[\s_-]*\d*$", re.IGNORECASE)


def _is_speaker_label(head: str) -> bool:
    """True for «Спикер 1» / «Иван Петров» before a colon, false for the start of a
    sentence («Обсудили сроки: …») — otherwise the title would lose its first words."""
    head = head.strip()
    if not head or len(head) > 40:
        return False
    if _SPEAKER_RE.match(head):
        return True
    words = head.split()
    return len(words) <= 3 and all(w[:1].isupper() for w in words)


def transcript_title(text: str | None) -> str | None:
    """First meaningful line of a transcript, stripped of the speaker/timecode prefix."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        # ASR lines look like «[00:00:04] Спикер 1: текст» — keep what was actually said.
        while line.startswith("["):
            end = line.find("]")
            if end < 0:
                break
            line = line[end + 1:].strip()
        head, sep, rest = line.partition(":")
        if sep and rest.strip() and _is_speaker_label(head):
            line = rest.strip()
        # Skip subtitle indices and timecode-only lines — they carry no title.
        if line and any(ch.isalpha() for ch in line):
            return line[:_TITLE_MAX_CHARS]
    return None


# --- Deterministic signals (§5): run on the RAW transcript, before folding ---

# «Это по проекту Крабы» is said at the start of the recording — roughly the first
# 15 seconds of speech.
MARKER_HEAD_CHARS = 1200


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _mentions(text: str, term: str) -> bool:
    """Whole-word occurrence of `term` in already-normalized `text`."""
    t = _norm(term)
    if not t:
        return False
    return re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text) is not None


def deterministic_matches(transcript: str, projects: list) -> dict[uuid.UUID, str]:
    """project_id -> why it is pinned. Two rules, both user-controlled:

    the project's NAME spoken in the opening seconds (the voice-marker convention), and
    any of its ALIASES anywhere in the recording. Aliases are opt-in per project, so a
    rule only ever fires on terms the user added deliberately — that predictability is
    the point (§5): the model cannot overturn it."""
    head = _norm(transcript[:MARKER_HEAD_CHARS])
    full = _norm(transcript)
    pinned: dict[uuid.UUID, str] = {}
    for p in projects:
        if _mentions(head, p.name):
            pinned[p.id] = f"проект назван в начале записи: «{p.name}»"
            continue
        for alias in p.aliases or []:
            if _mentions(full, alias):
                pinned[p.id] = f"ключевое слово проекта: «{alias}»"
                break
    return pinned


# --- Extracted items ---


# The title is part of uq_action_item_meeting, and Postgres' btree caps an index row at
# ~2.7KB — a runaway "title" from the model must not turn into a failed insert. A task
# title longer than this is not a title anyway.
_ITEM_TITLE_LIMIT = 300


# --- Сводка встречи: разделы «О чём / Обсудили / Договорились / Открытые вопросы» ---
# Модель пишет их размеченным текстом (ai/prompt.py, пункт 2); старые сводки — сплошной
# текст, они остаются одним блоком без заголовка.
_SUMMARY_HEAD = re.compile(
    r"^\**\s*(о чём|обсудили|договорились|решили|открытые вопросы)\s*\**\s*[:.]\s*\**\s*(.*)$",
    re.IGNORECASE,
)


@dataclass
class SummaryBlock:
    head: str | None
    text: list[str] = field(default_factory=list)
    points: list[str] = field(default_factory=list)


def summary_blocks(summary: str | None) -> list[SummaryBlock]:
    blocks: list[SummaryBlock] = []
    current: SummaryBlock | None = None
    for raw in (summary or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        head = _SUMMARY_HEAD.match(line)
        if head:
            name = head.group(1)
            current = SummaryBlock(name[0].upper() + name[1:].lower())
            blocks.append(current)
            if head.group(2).strip():
                current.text.append(head.group(2).strip())
            continue
        if current is None:
            current = SummaryBlock(None)
            blocks.append(current)
        if line[:2] in ("- ", "• ", "* ", "– "):
            current.points.append(line[2:].strip())
        else:
            current.text.append(line)
    return blocks


def summary_gist(summary: str | None) -> str:
    """Одна строка о встрече — для карточки задачи: раздел «О чём», у старой сводки — она вся."""
    blocks = summary_blocks(summary)
    about = next((b for b in blocks if b.head == "О чём"), None)
    first = about or (blocks[0] if blocks else None)
    return " ".join(first.text + first.points) if first else ""


def _title_key(title: str | None) -> str:
    return _norm(title)


def _cp_key(cp: str | None) -> str:
    return _norm(cp)


def _item_reason(item) -> str | None:
    """Model's reason, prefixed with the timecode so the note can point into the record."""
    at = (item.at or "").strip()
    reason = (item.reason or "").strip()
    if at and reason:
        return f"[{at}] {reason}"
    return f"[{at}]" if at else (reason or None)


async def _sync_items(session: AsyncSession, meeting: Meeting, items: list) -> dict:
    """Re-create this meeting's machine-made items from the latest extraction.

    A meeting is immutable, unlike a mail thread, so «delete and re-create» is the honest
    idempotency rule (§8.5). What the user touched is NOT deleted and NOT re-created over:
    both a dismissal and a completion are verdicts. Note that dismiss() sets user_edited
    while complete() only sets resolved_by='user' — filtering on user_edited alone would
    resurrect every task the user had ticked off."""
    rows = (
        await session.execute(
            select(ActionItem).where(ActionItem.source_meeting_id == meeting.id)
        )
    ).scalars().all()
    kept: list[ActionItem] = []
    for row in rows:
        # Прямая привязка к Нити — тоже решение (владельца или принятого предложения агента).
        if row.user_edited or row.resolved_by == "user" or row.project_id is not None:
            kept.append(row)
        else:
            await session.delete(row)
    await session.flush()

    taken = {(str(r.type), _cp_key(r.counterparty), _title_key(r.title)) for r in kept}
    created = 0
    for it in items:
        cp = (it.counterparty or "").strip() or None
        # A promise with nobody to keep it to is a task to myself (docs/ux-tasks.md §5).
        type_ = ActionType.TASK if it.type == ActionType.MY_COMMITMENT and cp is None else it.type
        key = (str(type_), _cp_key(cp), _title_key(it.title))
        if key in taken:
            continue  # the user already ruled on this exact item — don't argue
        session.add(
            ActionItem(
                source_kind=SourceKind.MEETING,
                space_id=meeting.space_id,
                type=type_,
                title=it.title.strip()[:_ITEM_TITLE_LIMIT],
                counterparty=cp,
                # A decision or a fact has no deadline; keeping one would drag it into
                # «Сегодня», where a type without a lifecycle has no business being.
                due_date=it.due_date if type_ in ACTIONABLE_TYPES else None,
                status=ActionStatus.OPEN,
                source_meeting_id=meeting.id,
                confidence=it.confidence,
                reason=_item_reason(it),
            )
        )
        taken.add(key)
        created += 1
    return {"items_created": created, "items_kept": len(kept)}


# --- Re-running the routing by hand ---
#
# The queue is not a to-do list: an event that returned early (no mailbox chosen, no
# project opted in, no model) is DONE as far as the worker is concerned. So fixing the
# setting afterwards changes nothing on its own — those meetings would sit «не разобрана»
# for ever. This is what the button on the «Встречи» screen re-runs.


async def routing_blocker(session: AsyncSession) -> str | None:
    """Why routing cannot run at all right now, or None. Global reasons only — the
    per-meeting one (no transcript) is checked while processing that meeting.

    Single source of truth: process_meeting_ready writes exactly these texts into
    Meeting.route_note, and the screen shows the same sentence before you press."""
    from email_reader import projects as projects_svc

    if not await projects_svc.list_for_meetings(session):
        return "Ни один проект не помечен «учитывать встречи» — раскладывать некуда."
    rm = await routing_model(session)
    if not rm.api_key:
        return "Не настроена модель для разбора встреч."
    return None


async def _latest_routing_events(session: AsyncSession, meeting_ids: set[str]) -> dict:
    """The last routing event of each of these meetings, newest first."""
    from email_reader.models import Event

    rows = (
        await session.execute(
            select(Event)
            .where(Event.type == str(EventType.MEETING_READY))
            .order_by(Event.created_at.desc(), Event.id.desc())
        )
    ).scalars().all()
    latest: dict = {}
    for ev in rows:
        mid = (ev.payload or {}).get("meeting_id")
        if mid in meeting_ids and mid not in latest:
            latest[mid] = ev
    return latest


async def last_routing_failure(session: AsyncSession, stuck: list[Meeting]) -> str | None:
    """Почему сорвалась последняя попытка разобрать эти встречи, или None.

    Отличается от `routing_blocker` тем, что это не настройка, а отказ на той стороне:
    кнопку не убираем — пополнили счёт, и следующее нажатие пройдёт. Смотрим последнее
    событие каждой встречи, а не последнее событие вообще: в пачке после нажатия последней
    оказывалась встреча с пустой расшифровкой, она «выполнена» — и отказ провайдера по
    остальным пропадал с экрана. Разобранная встреча сюда не попадает, поэтому
    починенная ошибка уходит сама."""
    from email_reader.ai.errors import explain

    latest = await _latest_routing_events(session, {str(m.id) for m in stuck})
    for ev in latest.values():
        said = explain((ev.payload or {}).get("last_error"))
        if ev.status == EventStatus.FAILED and said:
            return said
    return None


async def parked_meetings(session: AsyncSession) -> tuple[int, str | None]:
    """Сколько неразобранных встреч ждут, пока их модель снова ответит, и почему.

    Кнопка им не нужна: очередь вернёт их сама (worker._maybe_revive_stalled)."""
    from email_reader.ai.errors import explain

    ids = (
        await session.execute(
            select(Meeting.id).where(Meeting.routed_at.is_(None), Meeting.deleted_at.is_(None))
        )
    ).scalars().all()
    latest = await _latest_routing_events(session, {str(i) for i in ids})
    waiting = [ev for ev in latest.values() if ev.status == EventStatus.STALLED]
    if not waiting:
        return 0, None
    return len(waiting), explain((waiting[0].payload or {}).get("last_error"))


async def unprocessed_meetings(session: AsyncSession) -> list[Meeting]:
    """Never routed AND nothing in the queue is going to route them.

    Meetings whose event is still pending/processing are excluded on purpose: the worker
    will get to them, and re-queuing would pay for a second model call on the same
    recording. Parked ones (stalled) too: they come back once their model answers."""
    from email_reader.models import Event

    rows = list(
        (
            await session.execute(
                select(Meeting).where(Meeting.routed_at.is_(None), Meeting.deleted_at.is_(None))
                .order_by(Meeting.started_at.desc())
            )
        ).scalars().all()
    )
    if not rows:
        return []
    live = (
        await session.execute(
            select(Event.payload).where(
                Event.type == str(EventType.MEETING_READY),
                Event.status.in_(
                    [EventStatus.PENDING, EventStatus.PROCESSING, EventStatus.STALLED]
                ),
            )
        )
    ).scalars().all()
    queued = {(p or {}).get("meeting_id") for p in live}
    return [m for m in rows if str(m.id) not in queued]


async def requeue_unprocessed(session: AsyncSession) -> int:
    """Put MEETING_READY back for every meeting nothing else will pick up."""
    rows = await unprocessed_meetings(session)
    for m in rows:
        m.route_note = None      # the old reason is about to be re-decided
        await enqueue(session, EventType.MEETING_READY, {"meeting_id": str(m.id)})
    return len(rows)


# --- Routing (EventType.MEETING_READY) ---


async def process_meeting_ready(session: AsyncSession, payload: dict) -> None:
    """Fold the transcript, extract what was said, and decide which projects it belongs to.

    Everything extracted is stored regardless of the project verdict: a task spoken out
    loud is a fact about the meeting, not about a project (§6.1). Only writing into a
    project folder waits for a confirmed membership."""
    from email_reader import projects as projects_svc
    from email_reader import prompts
    from email_reader.ai.extractor import Usage, fold_transcript, route_meeting
    from email_reader.ai.schemas import MeetingRoutingResult

    meeting = await session.get(Meeting, uuid.UUID(payload["meeting_id"]))
    if meeting is None or meeting.deleted_at is not None:
        return
    transcript = await read_transcript(session, meeting)
    # The stand-in title is only assigned at the end of each path: a write before the model
    # calls holds SQLite's write lock through both of them (docs/feature-storage-plan.md).
    title = meeting.title or transcript_title(transcript)
    if not transcript or not transcript.strip():
        meeting.title = title
        meeting.route_note = (
            "Расшифровка не найдена — проверьте, где лежит архив записей."
            if transcript is None
            else "Расшифровка пустая — диктофон не записал текст, разбирать нечего."
        )
        return

    blocker = await routing_blocker(session)
    if blocker is not None:
        meeting.title = title
        meeting.route_note = blocker
        return
    # The meeting's own space: its projects, its model, its prompt.
    candidates = await projects_svc.list_for_meetings(session, meeting.space_id)
    if not candidates:
        meeting.title = title
        meeting.route_note = (
            "В этом пространстве ни один проект не помечен «учитывать встречи»."
        )
        return
    rm = await routing_model(session, meeting.space_id)

    pinned = deterministic_matches(transcript, candidates)
    # What the space knows around this meeting — the same builder the mail extractor
    # uses: the participants (by name; the model names them on the first pass, so a
    # re-route knows more), the owner's profile, the facts that touch the title and the
    # opening of the transcript. Reference data, never instructions.
    from email_reader import profiles
    from email_reader.ai.prompt import profile_block
    from email_reader.memory import context as memory_ctx

    memory = await memory_ctx.for_subject(
        session, space_id=meeting.space_id,
        people=[(None, str(p)) for p in (meeting.participants or [])],
        query=f"{title or ''} {transcript[:_MEMORY_QUERY_CHARS]}",
    )
    system_prompt = (
        await prompts.get_meeting_prompt(session, meeting.space_id)
        + profile_block(await profiles.core(session, meeting.space_id))
        + memory.render("route")
    )
    fold, fold_calls = await fold_transcript(
        transcript, kind=rm.kind, base_url=rm.base_url, api_key=rm.api_key, model=rm.model,
        no_thinking=rm.no_thinking,
    )
    call = dict(kind=rm.kind, base_url=rm.base_url, api_key=rm.api_key, model=rm.model,
                no_thinking=rm.no_thinking, system_prompt=system_prompt)
    # Два вызова: договорённости по всей встрече — отдельно от раскладки по проектам.
    said, usage = await route_meeting(fold, [], clock.today(), focus="items", **call)
    routed, usage_b = await route_meeting(
        fold,
        [{"name": p.name, "prompt": p.prompt} for p in candidates],
        clock.today(),
        hints=[p.name for p in candidates if p.id in pinned],
        focus="matches", **call,
    )
    usage = Usage(usage.tokens_in + usage_b.tokens_in, usage.tokens_out + usage_b.tokens_out)
    result = MeetingRoutingResult(
        title=said.title, summary=said.summary, participants=said.participants,
        items=said.items, matches=routed.matches,
    )
    # Пустой ответ — не «на встрече ничего не было»: рассуждающая модель съедает весь
    # лимит, а шлюз всё равно отдаёт вызов инструмента без аргументов. Принятый как есть,
    # он стирал машинные задачи встречи и её Нити при переразборе.
    said = (result.title or "").strip() or (result.summary or "").strip()
    if not said and not result.items and not result.matches:
        meeting.title = meeting.title or title
        meeting.route_note = (
            "Модель вернула пустой разбор — встреча не переразобрана, прежние задачи "
            "и Нити сохранены. Попробуйте ещё раз или другую модель."
        )
        return

    members, suggestions = _decide(result, candidates, pinned)
    digests = {m.project: m.digest for m in result.matches if m.digest}
    names = {p.id: p.name for p in candidates}
    for pid, (confidence, reason, source) in members.items():
        await projects_svc.assign_meeting(
            session, pid, meeting.id, source=source, confidence=confidence, reason=reason,
            digest=digests.get(names.get(pid, "")),
        )
    # Раскладка пришла пустой — это сбой вызова, а не «ни к чему не относится»: прежние
    # привязки встречи к Нитям не снимаем.
    removed = (
        await projects_svc.remove_stale_meeting_auto(session, meeting.id, keep=set(members))
        if result.matches else set()
    )

    stats = await _sync_items(session, meeting, result.items)
    # The first line of a transcript was only a stand-in until someone read the whole
    # meeting; the model's topic replaces it.
    if result.title and result.title.strip():
        meeting.title = result.title.strip()[:_TITLE_MAX_CHARS]
    else:
        meeting.title = title
    if result.summary:
        meeting.summary = result.summary
    if result.participants and not meeting.participants:
        meeting.participants = [str(p) for p in result.participants]
    meeting.suggestions = suggestions
    meeting.route_note = None
    meeting.routed_at = datetime.now(UTC)
    await session.flush()
    await search_index.index_meeting(session, meeting)

    # Disk last, and never fatal: a folder we can't write into must not cost us the
    # extraction that already landed in the database.
    export: dict = {}
    try:
        export = await projects_svc.export_meeting(session, meeting, removed=removed)
    except Exception:  # noqa: BLE001
        log.exception("Export of meeting %s to project folders failed", meeting.ext_id)

    try:
        from email_reader.notify import meeting_routed

        await meeting_routed(
            meeting.title or "Встреча",
            [names.get(pid, "?") for pid in members],
            len(suggestions),
            stats.get("items_created", 0),
        )
    except Exception:  # noqa: BLE001 — a notification is never worth failing the event
        log.warning("Could not notify about meeting %s", meeting.ext_id, exc_info=True)

    session.add(
        AgentDecision(
            kind="meeting_route",
            payload={
                "meeting_id": str(meeting.id),
                "projects": [str(p) for p in members],
                "pinned": [str(p) for p in pinned],
                "suggested": [s["project_id"] for s in suggestions],
                "fold_calls": fold_calls,
                "memory": memory.audit(),
                **stats,
                **export,
            },
            model=rm.model,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
        )
    )


def _decide(result, candidates: list, pinned: dict[uuid.UUID, str]):
    """Turn model verdicts + pinned rules into (memberships, suggestions), per §7.

    Confident (≥0.8 or pinned) files the meeting; the 0.5–0.8 zone only proposes and
    writes nothing to disk. The cap on automatic projects applies to the model's
    verdicts — a rule the user wrote themselves is not a guess and is never dropped."""
    by_name = {p.name: p for p in candidates}
    members: dict[uuid.UUID, tuple[float | None, str | None, str]] = {
        pid: (1.0, reason, "marker") for pid, reason in pinned.items()
    }
    ranked = sorted(
        (m for m in result.matches if m.member),
        key=lambda m: m.confidence,
        reverse=True,
    )
    suggestions: list[dict] = []
    for m in ranked:
        project = by_name.get(m.project)
        if project is None or project.id in members:
            continue
        if m.confidence >= AUTO_THRESHOLD:
            if len(members) >= MAX_AUTO_PROJECTS:
                continue
            members[project.id] = (m.confidence, m.reason, "auto")
        elif m.confidence >= SUGGEST_THRESHOLD:
            suggestions.append(
                {
                    "project_id": str(project.id),
                    "name": project.name,
                    "confidence": m.confidence,
                    "reason": m.reason,
                }
            )
    return members, suggestions


# --- Queries ---


def _unrouted_filter(q):
    """Meetings that belong to no project — the queue of things to look at by hand."""
    from email_reader.models import ProjectMeeting

    return q.where(
        ~select(ProjectMeeting.id)
        .where(ProjectMeeting.meeting_id == Meeting.id)
        .exists()
    )


async def list_meetings(
    session: AsyncSession, limit: int = 50, offset: int = 0, unrouted_only: bool = False,
    space_id: uuid.UUID | None = None,
) -> list[Meeting]:
    q = select(Meeting).where(Meeting.deleted_at.is_(None)).order_by(Meeting.started_at.desc())
    if space_id is not None:
        q = q.where(Meeting.space_id == space_id)
    if unrouted_only:
        q = _unrouted_filter(q)
    return list((await session.execute(q.limit(limit).offset(offset))).scalars().all())


async def count_meetings(
    session: AsyncSession, unrouted_only: bool = False, space_id: uuid.UUID | None = None
) -> int:
    q = select(func.count()).select_from(Meeting).where(Meeting.deleted_at.is_(None))
    if space_id is not None:
        q = q.where(Meeting.space_id == space_id)
    if unrouted_only:
        q = _unrouted_filter(q)
    return (await session.execute(q)).scalar_one()


async def get_meeting(session: AsyncSession, meeting_id: uuid.UUID) -> Meeting | None:
    return (
        await session.execute(select(Meeting).where(Meeting.id == meeting_id))
    ).scalar_one_or_none()
