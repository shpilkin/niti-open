"""The search index: one FTS5 table for every kind of thing Niti holds.

Shape — two tables that share a rowid:

* `search_docs` — plain SQL: kind, ref (what to open) and `at`, which the date filters
  and the «by date» order run on. Filters that are not text live here, where an index
  can help.
* `search_index` — FTS5 with `title`, `body`, `people` and the stem twins of the first
  two. Keeps its own copy of the text because `snippet()` needs one.

Why Python writes it instead of SQL triggers (as `emails_fts` was written): SQLite cannot
compute a Russian stem. The cost of that choice is that a new writer of indexed text can
be forgotten — `reconcile()` is the guard, it adds what is missing and drops what is
gone, and `reindex()` rebuilds the lot.

The space of a document is deliberately NOT stored: mailboxes and meetings move between
spaces, and a denormalised copy would go stale on the move. Scoping happens when the hits
are hydrated (`search.query`), against the source row.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from email_reader import meetings as meetings_svc
from email_reader.memory import facts
from email_reader.models import (
    ActionItem,
    CalendarEvent,
    Email,
    Meeting,
    MemoryFact,
    Person,
    PersonIdentity,
    Project,
)
from email_reader.search.text import clip, stems_of

# kind -> (Russian label, plural label). The order is the order of result groups.
KINDS: dict[str, tuple[str, str]] = {
    "mail": ("Письмо", "Письма"),
    "card": ("Обязательство", "Обязательства"),
    "meeting": ("Встреча", "Встречи"),
    "turn": ("Момент встречи", "Моменты встреч"),
    "event": ("Событие", "Календарь"),
    "person": ("Контакт", "Контакты"),
    "project": ("Проект", "Проекты"),
    "fact": ("Факт", "Память"),
}

DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS search_docs ("
    "  rowid INTEGER PRIMARY KEY,"
    "  kind TEXT NOT NULL,"
    "  ref TEXT NOT NULL,"
    "  at TEXT)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_search_docs_ref ON search_docs(kind, ref)",
    "CREATE INDEX IF NOT EXISTS ix_search_docs_at ON search_docs(kind, at)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5("
    "  title, body, people, title_stem, body_stem,"
    "  tokenize='unicode61 remove_diacritics 2')",
)

# Cap for a transcript that carries no timecodes at all: without them there is nothing to
# split on, and one document per meeting-length wall of text is useless to rank.
_TURN_CHARS = 600


@dataclass(slots=True)
class Doc:
    """One indexable thing. `ref` is what the UI opens: an id, or `<meeting>:<seconds>`."""

    kind: str
    ref: str
    at: datetime | None
    title: str = ""
    body: str = ""
    people: str = ""

    def stored_at(self) -> str | None:
        return iso(self.at)

    def columns(self) -> dict:
        title, body = self.title.strip(), clip(self.body)
        return {
            "title": title,
            "body": body,
            "people": self.people.strip(),
            "title_stem": stems_of(title),
            "body_stem": stems_of(body),
        }


def iso(value: datetime | None) -> str | None:
    """One spelling for every timestamp in the index: UTC, no offset, «T» separated.

    Date filters and the recency term are plain string / `julianday()` work on this
    column, and both are only correct while every row is written the same way.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.replace(tzinfo=None).isoformat(timespec="seconds")


def stamped(obj, name: str) -> datetime | None:  # noqa: ANN001
    """A timestamp column, but only if it is already loaded.

    `created_at` carries a server default, so straight after a flush it is unloaded and
    touching it starts a lazy refresh — which raises inside an async session. A document
    written at creation time is dated now anyway.
    """
    from sqlalchemy import inspect as sa_inspect

    if name in sa_inspect(obj).unloaded:
        return None
    return getattr(obj, name, None)


def _addr_line(entries: list | None) -> str:
    """«Имя <адрес>» pairs out of a JSON address column."""
    out: list[str] = []
    for e in entries or []:
        if isinstance(e, dict):
            out += [str(e.get("name") or ""), str(e.get("email") or "")]
        elif e:
            out.append(str(e))
    return " ".join(p for p in out if p)


# --- builders: one per kind -----------------------------------------------------------


def doc_for_mail(email: Email) -> Doc:
    people = " ".join(
        p for p in (email.from_name or "", email.from_addr or "",
                    _addr_line(email.to_addrs), _addr_line(email.cc_addrs)) if p
    )
    return Doc(kind="mail", ref=email.id.hex, at=email.sent_at, title=email.subject or "",
               body=email.body_text or "", people=people)


def doc_for_card(item: ActionItem) -> Doc:
    people = " ".join(p for p in (item.counterparty or "", item.delegate_to or "") if p)
    return Doc(kind="card", ref=item.id.hex, at=stamped(item, "created_at"),
               title=item.title or "", body=item.reason or "", people=people)


def doc_for_meeting(meeting: Meeting) -> Doc:
    people = " ".join(
        str(p.get("name") or p.get("speaker") or p) if isinstance(p, dict) else str(p)
        for p in (meeting.participants or [])
    )
    return Doc(kind="meeting", ref=meeting.id.hex, at=meeting.started_at,
               title=meeting.title or "", body=meeting.summary or "", people=people)


def docs_for_turns(meeting: Meeting, transcript: str | None) -> list[Doc]:
    """The transcript cut into windows, each carrying the timecode it starts at, so a hit
    can open the transcript at that moment (`meetings.timecode_span`)."""
    out: list[Doc] = []
    buf: list[str] = []
    seconds = 0

    def flush() -> None:
        if buf:
            out.append(Doc(kind="turn", ref=f"{meeting.id.hex}:{seconds}",
                           at=meeting.started_at, title=meeting.title or "",
                           body=" ".join(buf), people=""))
            buf.clear()

    for raw in (transcript or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        found = meetings_svc.STAMP_RE.match(line)
        # A timecoded line is an utterance and starts its own document, so a hit knows
        # the second it was said. Lines without one are continuations of the last.
        if found or sum(len(b) for b in buf) >= _TURN_CHARS:
            flush()
        if found and not buf:
            seconds = meetings_svc.stamp_seconds(found.group(0))
        buf.append(line)
    flush()
    return out


def doc_for_event(event: CalendarEvent) -> Doc:
    people = " ".join(
        p for p in (event.organizer or "", _addr_line(event.attendees)) if p
    )
    body = " ".join(p for p in (event.description or "", event.location or "") if p)
    return Doc(kind="event", ref=event.id.hex, at=event.start_at, title=event.title or "",
               body=body, people=people)


def doc_for_person(person: Person, identities: list[PersonIdentity], notes: str = "") -> Doc:
    who = [person.name or "", person.email or ""]
    who += [i.value for i in identities] + [i.display or "" for i in identities]
    return Doc(kind="person", ref=person.id.hex, at=person.updated_at,
               title=person.name or person.email or "", body=notes or "",
               people=" ".join(p for p in who if p))


def doc_for_fact(fact: MemoryFact, who: str = "") -> Doc:
    """One fact of the owner's memory. `who` is the person it is about, so a name finds
    what is remembered about them; a fact about me has nobody in that column."""
    return Doc(kind="fact", ref=fact.id.hex,
               at=fact.valid_from or stamped(fact, "created_at"),
               title=fact.text, people=who)


def doc_for_project(project: Project) -> Doc:
    aliases = " ".join(str(a) for a in (project.aliases or []))
    return Doc(kind="project", ref=project.id.hex, at=stamped(project, "created_at"),
               title=project.name, body=project.prompt or "", people=aliases)


# --- store ----------------------------------------------------------------------------


async def put(session: AsyncSession, docs: Doc | list[Doc]) -> int:
    """Insert or replace documents. Returns how many rows were written."""
    items = [docs] if isinstance(docs, Doc) else list(docs)
    for doc in items:
        row = (await session.execute(
            text("SELECT rowid FROM search_docs WHERE kind = :k AND ref = :r"),
            {"k": doc.kind, "r": doc.ref},
        )).scalar_one_or_none()
        cols = doc.columns()
        if row is not None:
            await session.execute(text("DELETE FROM search_index WHERE rowid = :rid"),
                                  {"rid": row})
            await session.execute(
                text("UPDATE search_docs SET at = :at WHERE rowid = :rid"),
                {"at": doc.stored_at(), "rid": row},
            )
        else:
            await session.execute(
                text("INSERT INTO search_docs(kind, ref, at) VALUES (:k, :r, :at)"),
                {"k": doc.kind, "r": doc.ref, "at": doc.stored_at()},
            )
            row = (await session.execute(
                text("SELECT rowid FROM search_docs WHERE kind = :k AND ref = :r"),
                {"k": doc.kind, "r": doc.ref},
            )).scalar_one()
        await session.execute(
            text("INSERT INTO search_index(rowid, title, body, people, title_stem, "
                 "body_stem) VALUES (:rid, :title, :body, :people, :title_stem, "
                 ":body_stem)"),
            {"rid": row, **cols},
        )
    return len(items)


async def drop(session: AsyncSession, kind: str, ref: str) -> None:
    row = (await session.execute(
        text("SELECT rowid FROM search_docs WHERE kind = :k AND ref = :r"),
        {"k": kind, "r": ref},
    )).scalar_one_or_none()
    if row is None:
        return
    await session.execute(text("DELETE FROM search_index WHERE rowid = :rid"), {"rid": row})
    await session.execute(text("DELETE FROM search_docs WHERE rowid = :rid"), {"rid": row})


async def drop_prefix(session: AsyncSession, kind: str, prefix: str) -> None:
    """Every document of a kind whose ref starts with `prefix` — the turns of one
    meeting, whose count changes when the transcript is re-read."""
    rows = [r[0] for r in (await session.execute(
        text("SELECT rowid FROM search_docs WHERE kind = :k AND ref LIKE :p"),
        {"k": kind, "p": f"{prefix}%"},
    )).all()]
    for row in rows:
        await session.execute(text("DELETE FROM search_index WHERE rowid = :rid"), {"rid": row})
        await session.execute(text("DELETE FROM search_docs WHERE rowid = :rid"), {"rid": row})


# --- what the app calls on a write ----------------------------------------------------


async def index_mail(session: AsyncSession, email: Email) -> None:
    await put(session, doc_for_mail(email))


async def index_card(session: AsyncSession, item: ActionItem) -> None:
    await put(session, doc_for_card(item))


async def index_event(session: AsyncSession, event: CalendarEvent) -> None:
    await put(session, doc_for_event(event))


async def index_project(session: AsyncSession, project: Project) -> None:
    await put(session, doc_for_project(project))


async def index_fact(session: AsyncSession, fact: MemoryFact, who: str = "") -> None:
    await put(session, doc_for_fact(fact, who))


async def index_person(session: AsyncSession, person: Person) -> None:
    ids = list((await session.execute(
        select(PersonIdentity).where(PersonIdentity.person_id == person.id)
    )).scalars())
    await put(session, doc_for_person(person, ids, await facts.notes_of(session, person.id)))


async def index_meeting(session: AsyncSession, meeting: Meeting, *, turns: bool = True) -> None:
    """The meeting itself plus, when the transcript is readable, its moments.

    A transcript is a file and files move or vanish; a missing one costs the moments,
    never the meeting.
    """
    await put(session, doc_for_meeting(meeting))
    if not turns:
        return
    await drop_prefix(session, "turn", f"{meeting.id.hex}:")
    try:
        transcript = await meetings_svc.read_transcript(session, meeting)
    except OSError:
        transcript = None
    if transcript:
        await put(session, docs_for_turns(meeting, transcript))


# --- bulk: reindex and reconcile ------------------------------------------------------

# Only the columns a document needs. An Email carries `body_raw_html` (often many times
# larger than the plain text) — loading whole rows for a rebuild would read
# the entire store into memory.
_SELECTS: dict[str, tuple] = {
    "mail": (Email, (Email.id, Email.subject, Email.body_text, Email.from_name,
                     Email.from_addr, Email.to_addrs, Email.cc_addrs, Email.sent_at,
                     Email.created_at)),
    "card": (ActionItem, (ActionItem.id, ActionItem.title, ActionItem.reason,
                          ActionItem.counterparty, ActionItem.delegate_to,
                          ActionItem.created_at)),
    "meeting": (Meeting, (Meeting.id, Meeting.title, Meeting.summary, Meeting.participants,
                          Meeting.started_at, Meeting.transcript_path)),
    "event": (CalendarEvent, (CalendarEvent.id, CalendarEvent.title,
                              CalendarEvent.description, CalendarEvent.location,
                              CalendarEvent.organizer, CalendarEvent.attendees,
                              CalendarEvent.start_at)),
    "person": (Person, (Person.id, Person.name, Person.email, Person.updated_at)),
    "project": (Project, (Project.id, Project.name, Project.prompt, Project.aliases,
                          Project.created_at)),
    "fact": (MemoryFact, (MemoryFact.id, MemoryFact.text, MemoryFact.subject_kind,
                          MemoryFact.subject_key, MemoryFact.valid_from,
                          MemoryFact.valid_to, MemoryFact.status, MemoryFact.created_at)),
}

# Rows of a model that are documents. A retired fact stays in its table (memory keeps
# its history) but is not a search result, so the sweeps must not count it as missing.
_LIVE = {
    "fact": lambda q: q.where(MemoryFact.status == "active", MemoryFact.valid_to.is_(None)),
}

_BUILDERS = {
    "mail": doc_for_mail,
    "card": doc_for_card,
    "meeting": doc_for_meeting,
    "event": doc_for_event,
    "project": doc_for_project,
}


async def _stream(session: AsyncSession, kind: str):
    model, cols = _SELECTS[kind]
    q = select(model).options(load_only(*cols[1:]))
    if kind in _LIVE:
        q = _LIVE[kind](q)
    return list((await session.execute(q)).scalars())


async def _identity_map(session: AsyncSession) -> dict[uuid.UUID, list[PersonIdentity]]:
    out: dict[uuid.UUID, list[PersonIdentity]] = {}
    for ident in (await session.execute(select(PersonIdentity))).scalars():
        out.setdefault(ident.person_id, []).append(ident)
    return out


async def _docs_of(session: AsyncSession, kind: str, obj) -> list[Doc]:  # noqa: ANN001
    """Documents for one entity — a meeting yields its own row plus its moments."""
    if kind == "person":
        ids = list((await session.execute(
            select(PersonIdentity).where(PersonIdentity.person_id == obj.id)
        )).scalars())
        return [doc_for_person(obj, ids, await facts.notes_of(session, obj.id))]
    if kind == "fact":
        return [doc_for_fact(obj, await facts.who_of(session, obj))]
    if kind == "meeting":
        docs = [doc_for_meeting(obj)]
        try:
            transcript = await meetings_svc.read_transcript(session, obj)
        except OSError:
            transcript = None
        if transcript:
            docs += docs_for_turns(obj, transcript)
        return docs
    return [_BUILDERS[kind](obj)]


# Пересборка идёт пачками, каждая своей транзакцией: одним заходом 17 тыс. документов
# держали замок базы 13 с, и всё это время никто другой писать не мог
# (docs/feature-storage-plan.md).
BUILD_BATCH = 1000
BUILD_ROUNDS = 500

log = logging.getLogger("email_reader.search")


async def rebuild(scope) -> int:
    """Полная пересборка индекса пачками. `scope` — фабрика сессий (`db.session_scope`):
    очистка и каждая пачка идут своей транзакцией. Возвращает, сколько записей добавлено."""
    for kind in (*_SELECTS, "turn"):
        async with scope() as session:
            await _clear(session, kind)
    added = 0
    for _ in range(BUILD_ROUNDS):
        async with scope() as session:
            batch = await reconcile(session, limit=BUILD_BATCH)
        added += batch["added"]
        if not batch["added"]:
            break
    else:
        log.warning("Search index still incomplete after %d batches", BUILD_ROUNDS)
    return added


async def _clear(session: AsyncSession, kind: str) -> None:
    await session.execute(
        text("DELETE FROM search_index WHERE rowid IN "
             "(SELECT rowid FROM search_docs WHERE kind = :k)"), {"k": kind},
    )
    await session.execute(text("DELETE FROM search_docs WHERE kind = :k"), {"k": kind})


async def reindex(session: AsyncSession, kind: str | None = None) -> dict[str, int]:
    """Rebuild from scratch — the «Переиндексировать» button and the `reindex` CLI."""
    kinds = [kind] if kind else list(_SELECTS)
    written: dict[str, int] = {}
    idents = await _identity_map(session) if "person" in kinds else {}
    notes = await facts.notes_map(session) if "person" in kinds else {}
    for k in kinds:
        await _clear(session, k)
        if k == "meeting":
            await _clear(session, "turn")
        n = 0
        for obj in await _stream(session, k):
            docs = ([doc_for_person(obj, idents.get(obj.id, []), notes.get(obj.id.hex, ""))]
                    if k == "person" else await _docs_of(session, k, obj))
            n += await put(session, docs)
        written[k] = n
    return written


async def _all_ids(session: AsyncSession, kind: str) -> set[str]:
    model, _ = _SELECTS[kind]
    q = select(model.id)
    if kind in _LIVE:
        q = _LIVE[kind](q)
    return {r.hex for r in (await session.execute(q)).scalars()}


async def _indexed_refs(session: AsyncSession, kind: str) -> set[str]:
    return {r[0] for r in (await session.execute(
        text("SELECT ref FROM search_docs WHERE kind = :k"), {"k": kind}
    )).all()}


async def reconcile(session: AsyncSession, *, limit: int = 500) -> dict[str, int]:
    """Add what is missing, drop what is gone.

    The safety net under the hand-placed `index_*` calls: a writer nobody wired up shows
    up here as a missing document instead of a silent hole in the search.
    """
    added = removed = 0
    for kind in _SELECTS:
        live = await _all_ids(session, kind)
        have = await _indexed_refs(session, kind)
        for ref in sorted(have - live):
            await drop(session, kind, ref)
            if kind == "meeting":
                await drop_prefix(session, "turn", f"{ref}:")
            removed += 1
        missing = sorted(live - have)[:limit]
        for ref in missing:
            model, _ = _SELECTS[kind]
            obj = await session.get(model, uuid.UUID(hex=ref))
            if obj is not None:
                await put(session, await _docs_of(session, kind, obj))
                added += 1
    live_meetings = await _all_ids(session, "meeting")
    for ref in sorted(await _indexed_refs(session, "turn")):
        if ref.split(":", 1)[0] not in live_meetings:
            await drop(session, "turn", ref)
            removed += 1
    return {"added": added, "removed": removed}


async def counts(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(
        text("SELECT kind, count(*) FROM search_docs GROUP BY kind")
    )).all()
    return {k: n for k, n in rows}
