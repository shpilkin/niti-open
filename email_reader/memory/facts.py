"""What the owner knows, one row at a time — the store behind the space profile and the
notes on a person (docs/feature-memory-facts.md §3).

Two subjects for now. `me` is per space (role, teams, how things are done here) and
`person` is global (who someone is does not depend on which mailbox they wrote to,
docs/feature-memory.md §2.1). Evidence says where a row came from: the profile editor,
the person pane, «Запомнить», or — through `memory.capture` — one of the owner's own
letters or a moment of a meeting. Nothing is deleted: retiring a fact sets `valid_to`,
and a replacement points back through `supersedes_id`, so what the assistant knew last
month is still there to look at.

Status is the owner's word. What the owner wrote or remembered is `active` at once; what
capture found is `proposed` until the owner says yes (`confirm`) or no (`reject`), and
only `active` ever reaches a prompt (§6). A rejected or forgotten fact is remembered as
such, so the same thing is not proposed again (`propose` checks by Russian stems, the
same way search matches).

The rows render back into the same text the prompts got before facts existed — the
profile fact first, then «Запомнить» lines as `- date: text`; a person's lines in the order
they were written. That equality is what lets the change go in without re-measuring the
prompts.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.models import MemoryFact, Person
from email_reader.search.text import normalize, stem, tokens

ME = "me"
PERSON = "person"

EV_PROFILE = "profile"    # the settings editor: one fact holding the whole text
EV_NOTES = "notes"        # the person pane: one fact per line
EV_REMEMBER = "remember"  # «Запомнить» on a card, MCP `remember`: dated
EV_MAIL = "mail"          # capture: one of my outbound letters (evidence_id = its id)
EV_TURN = "turn"          # capture: a moment of a transcript (evidence_id = <meeting>:<sec>)
# Facts that carry the date they were learned on, and render with it.
_DATED = {EV_REMEMBER, EV_MAIL, EV_TURN}

ACTIVE = "active"
PROPOSED = "proposed"
REJECTED = "rejected"

# The closed list a captured fact is typed with (§3.1), and how the screen names each.
KINDS: dict[str, str] = {
    "role": "роль",
    "responsibility": "зона ответственности",
    "member": "состав команды",
    "preference": "предпочтение",
    "rule": "правило",
    "decision": "решение",
    "fact": "факт",
}
# One of these holds at a time: confirming a new one retires the old (§3.3).
_SINGLE_VALUED = {"role"}
# A live fact nobody has vouched for this long is shown as «устарело?» (§3.5) — shown,
# never retired by code.
STALE_AFTER = timedelta(days=180)
# Past this many live facts about one subject the screen asks the owner to prune; the
# prompt's own section budget is about that many lines anyway (memory.context).
SUBJECT_CAP = 12
# Two texts are the same fact when the shorter one's stems are mostly in the longer one's.
_SIMILAR = 0.7
_STEM_MIN_CHARS = 3
# The profile editor's fact always renders first; everything else gets the next number.
_PROFILE_SEQ = 0


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def person_key(person_id: uuid.UUID) -> str:
    return person_id.hex


def _subject(q, subject_kind: str, subject_key: str | None, space_id: uuid.UUID | None):  # noqa: ANN001
    q = q.where(MemoryFact.subject_kind == subject_kind)
    q = q.where(MemoryFact.subject_key.is_(None)) if subject_key is None else q.where(
        MemoryFact.subject_key == subject_key
    )
    return q.where(MemoryFact.space_id.is_(None)) if space_id is None else q.where(
        MemoryFact.space_id == space_id
    )


async def active(
    session: AsyncSession, *, subject_kind: str, subject_key: str | None = None,
    space_id: uuid.UUID | None = None,
) -> list[MemoryFact]:
    """The current facts of a subject, in render order."""
    q = _subject(select(MemoryFact), subject_kind, subject_key, space_id).where(
        MemoryFact.status == ACTIVE, MemoryFact.valid_to.is_(None)
    ).order_by(MemoryFact.seq, MemoryFact.created_at)
    return list((await session.execute(q)).scalars().all())


async def _next_seq(
    session: AsyncSession, subject_kind: str, subject_key: str | None, space_id: uuid.UUID | None
) -> int:
    # Over every row of the subject, retired ones included, so a number is never reused.
    q = _subject(select(func.max(MemoryFact.seq)), subject_kind, subject_key, space_id)
    top = (await session.execute(q)).scalar()
    return (top if top is not None else _PROFILE_SEQ) + 1


async def add(
    session: AsyncSession, *, subject_kind: str, text: str, evidence_kind: str,
    subject_key: str | None = None, space_id: uuid.UUID | None = None,
    evidence_id: str | None = None, supersedes: MemoryFact | None = None,
    seq: int | None = None, now: datetime | None = None,
) -> MemoryFact:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty fact")
    now = _now(now)
    fact = MemoryFact(
        space_id=space_id, subject_kind=subject_kind, subject_key=subject_key, text=text,
        evidence_kind=evidence_kind, evidence_id=evidence_id, status=ACTIVE,
        valid_from=now, created_at=now,
        supersedes_id=supersedes.id if supersedes is not None else None,
        seq=seq if seq is not None else await _next_seq(
            session, subject_kind, subject_key, space_id
        ),
    )
    session.add(fact)
    await session.flush()
    await _sync(session, fact)
    return fact


async def retire(session: AsyncSession, fact: MemoryFact, now: datetime | None = None) -> None:
    fact.valid_to = _now(now)
    await _sync(session, fact)


async def who_of(session: AsyncSession, fact: MemoryFact) -> str:
    """The people column of a fact's search document: the person it is about."""
    if fact.subject_kind != PERSON or not fact.subject_key:
        return ""
    person = await session.get(Person, uuid.UUID(hex=fact.subject_key))
    if person is None:
        return ""
    return " ".join(p for p in (person.name or "", person.email or "") if p)


async def _sync(session: AsyncSession, fact: MemoryFact) -> None:
    """Keep the search index in step with one fact: a live fact is a document, a retired
    one is not, and a person's own document carries their rendered notes, so it is
    refreshed too. Every change to a fact goes through this module, so this is the one
    place the index needs to hear about it."""
    from email_reader.search import index as search_index

    if fact.status == ACTIVE and fact.valid_to is None:
        await search_index.index_fact(session, fact, await who_of(session, fact))
    else:
        await search_index.drop(session, "fact", fact.id.hex)
    if fact.subject_kind == PERSON and fact.subject_key:
        person = await session.get(Person, uuid.UUID(hex=fact.subject_key))
        if person is not None:
            await search_index.index_person(session, person)


async def for_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, MemoryFact]:
    wanted = list(ids)
    if not wanted:
        return {}
    rows = (await session.execute(select(MemoryFact).where(MemoryFact.id.in_(wanted)))).scalars()
    return {f.id: f for f in rows}


def line_of(fact: MemoryFact) -> str:
    """The fact as one line of the free text it stands in for."""
    if fact.evidence_kind in _DATED and fact.valid_from is not None:
        stamp = f"{fact.valid_from:%Y-%m-%d}: {fact.text}"
        return f"- {stamp}" if fact.subject_kind == ME else stamp
    return fact.text


def render(facts: Iterable[MemoryFact]) -> str:
    return "\n".join(line_of(f) for f in facts)


async def set_lines(
    session: AsyncSession, *, subject_kind: str, text: str, evidence_kind: str,
    subject_key: str | None = None, space_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> list[MemoryFact]:
    """Editor semantics for a subject kept as one fact per line: a line that is still
    there keeps its row (and its date), a line that went away is retired, a new line
    becomes a new fact. Order is the order the lines were first written — moving lines
    around in the editor does not move rows."""
    now = _now(now)
    wanted = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    remaining = await active(
        session, subject_kind=subject_kind, subject_key=subject_key, space_id=space_id
    )
    kept: list[MemoryFact] = []
    for line in wanted:
        match = next((f for f in remaining if line_of(f) == line), None)
        if match is not None:
            remaining.remove(match)
            kept.append(match)
        else:
            kept.append(await add(
                session, subject_kind=subject_kind, subject_key=subject_key,
                space_id=space_id, text=line, evidence_kind=evidence_kind, now=now,
            ))
    for fact in remaining:
        await retire(session, fact, now)
    return kept


# --- people -------------------------------------------------------------------------


async def notes_of(session: AsyncSession, person_id: uuid.UUID) -> str:
    return render(await active(session, subject_kind=PERSON, subject_key=person_key(person_id)))


async def notes_map(session: AsyncSession) -> dict[str, str]:
    """person key -> rendered notes, for every person with any — one query, for the
    places that walk the whole address book (the people list, reindex)."""
    q = select(MemoryFact).where(
        MemoryFact.subject_kind == PERSON, MemoryFact.status == ACTIVE,
        MemoryFact.valid_to.is_(None),
    ).order_by(MemoryFact.subject_key, MemoryFact.seq, MemoryFact.created_at)
    lines: dict[str, list[str]] = {}
    for fact in (await session.execute(q)).scalars():
        lines.setdefault(fact.subject_key or "", []).append(line_of(fact))
    return {key: "\n".join(v) for key, v in lines.items()}


async def people_with_notes(
    session: AsyncSession, person_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    keys = {person_key(pid) for pid in person_ids}
    if not keys:
        return set()
    q = select(MemoryFact.subject_key).where(
        MemoryFact.subject_kind == PERSON, MemoryFact.status == ACTIVE,
        MemoryFact.valid_to.is_(None), MemoryFact.subject_key.in_(keys),
    ).distinct()
    return {uuid.UUID(hex=k) for k in (await session.execute(q)).scalars() if k}


async def move_person(
    session: AsyncSession, *, from_id: uuid.UUID, to_id: uuid.UUID
) -> list[dict]:
    """Re-point every fact (retired ones too) of one profile at another — what a merge
    does with memory. Returns what it takes to put them back: id and the old order."""
    q = select(MemoryFact).where(
        MemoryFact.subject_kind == PERSON, MemoryFact.subject_key == person_key(from_id)
    ).order_by(MemoryFact.seq, MemoryFact.created_at)
    moved = list((await session.execute(q)).scalars().all())
    if not moved:
        return []
    base = await _next_seq(session, PERSON, person_key(to_id), None)
    undo = []
    for offset, fact in enumerate(moved):
        undo.append({"id": str(fact.id), "seq": fact.seq})
        fact.subject_key = person_key(to_id)
        fact.seq = base + offset
    await session.flush()
    for fact in moved:
        await _sync(session, fact)
    return undo


async def restore_person(session: AsyncSession, *, person_id: uuid.UUID, rows: list[dict]) -> None:
    """Undo `move_person` from its payload."""
    restored = []
    for row in rows:
        fact = await session.get(MemoryFact, uuid.UUID(row["id"]))
        if fact is not None:
            fact.subject_key = person_key(person_id)
            fact.seq = int(row["seq"])
            restored.append(fact)
    await session.flush()
    for fact in restored:
        await _sync(session, fact)


# --- capture: proposals, the owner's verdict, aging ----------------------------------


def stems(text: str | None) -> set[str]:
    return {stem(t) for t in tokens(text) if len(t) >= _STEM_MIN_CHARS}


def similar(a: str, b: str) -> bool:
    """The same thing said in other words: the shorter text's stems are mostly in the
    longer one's. Containment, not Jaccard, so «Борис ведёт бэкенд» is the same fact as
    «Борис ведёт бэкенд и интеграции» — a longer restatement is not news."""
    sa, sb = stems(a), stems(b)
    if not sa or not sb:
        return normalize(a).split() == normalize(b).split()
    return len(sa & sb) / min(len(sa), len(sb)) >= _SIMILAR


def _name_key(name: str | None) -> str:
    return " ".join(normalize(name).split())


async def rows_of(
    session: AsyncSession, *, subject_kind: str, subject_key: str | None = None,
    space_id: uuid.UUID | None = None, subject_name: str | None = None,
) -> list[MemoryFact]:
    """Every row of a subject, whatever its status — what a new proposal is checked
    against. A person not in the address book is a subject by name."""
    q = _subject(select(MemoryFact), subject_kind, subject_key, space_id)
    rows = list((await session.execute(q)).scalars().all())
    if subject_kind == PERSON and subject_key is None:
        rows = [r for r in rows if _name_key(r.subject_name) == _name_key(subject_name)]
    return rows


async def propose(
    session: AsyncSession, *, subject_kind: str, kind: str, text: str, evidence_kind: str,
    evidence_id: str, quote: str, captured_in: uuid.UUID, confidence: float | None = None,
    subject_key: str | None = None, subject_name: str | None = None,
    space_id: uuid.UUID | None = None, now: datetime | None = None,
) -> tuple[str, MemoryFact]:
    """A captured fact, subject to the owner's yes. Says what happened: `proposed` (a
    new row, not in any prompt or index until confirmed), `seen` (a live row already says
    it — its `last_seen_at` is now), or `known` (a rejected or forgotten row says it —
    the owner has answered this one, nothing is written)."""
    if kind not in KINDS:
        raise ValueError(f"unknown fact kind {kind!r}")
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("empty fact")
    now = _now(now)
    for row in await rows_of(
        session, subject_kind=subject_kind, subject_key=subject_key, space_id=space_id,
        subject_name=subject_name,
    ):
        # The editor's text is many facts in one row: a proposal is the same as one of
        # its lines, not as the blob — «Оля ушла из аналитики в дизайн» shares most of
        # its stems with a profile that lists both teams and is still news.
        lines = row.text.splitlines() if row.evidence_kind == EV_PROFILE else [row.text]
        if not any(similar(line, text) for line in lines if line.strip()):
            continue
        if row.valid_to is None and row.status in (ACTIVE, PROPOSED):
            row.last_seen_at = now
            return "seen", row
        return "known", row
    fact = MemoryFact(
        space_id=space_id, subject_kind=subject_kind, subject_key=subject_key,
        subject_name=subject_name, kind=kind, text=text, evidence_kind=evidence_kind,
        evidence_id=evidence_id, quote=quote, confidence=confidence, captured_in=captured_in,
        status=PROPOSED, created_at=now,
        seq=await _next_seq(session, subject_kind, subject_key, space_id),
    )
    session.add(fact)
    await session.flush()
    return "proposed", fact


async def confirm(session: AsyncSession, fact: MemoryFact, now: datetime | None = None) -> None:
    """The owner's yes. A proposal goes live — dated today, indexed, and if its kind holds
    one value at a time, the fact it replaces is retired. A yes on a fact that is already
    live just says it still holds («всё ещё так»)."""
    now = _now(now)
    if fact.status == ACTIVE and fact.valid_to is None:
        fact.confirmed_at = now
        return
    if fact.status != PROPOSED:
        raise ValueError("only a proposal can be confirmed")
    if fact.subject_kind == PERSON and not fact.subject_key:
        raise ValueError("a fact about a person needs the person first")
    if fact.kind in _SINGLE_VALUED:
        for old in await active(
            session, subject_kind=fact.subject_kind, subject_key=fact.subject_key,
            space_id=fact.space_id,
        ):
            if old.kind == fact.kind and old.id != fact.id:
                await retire(session, old, now)
                fact.supersedes_id = old.id
    fact.status = ACTIVE
    fact.valid_from = now
    fact.confirmed_at = now
    await session.flush()
    await _sync(session, fact)


async def reject(session: AsyncSession, fact: MemoryFact, now: datetime | None = None) -> None:
    """The owner's no: kept, so the same thing is not proposed again."""
    if fact.status != PROPOSED:
        raise ValueError("only a proposal can be rejected")
    fact.status = REJECTED
    fact.valid_to = _now(now)


def _from_space(space_id: uuid.UUID):
    """Facts a space's screen answers for: about me here, and about people when the
    evidence came from this space's letters or meetings."""
    return or_(
        and_(MemoryFact.subject_kind == ME, MemoryFact.space_id == space_id),
        and_(MemoryFact.subject_kind == PERSON, MemoryFact.captured_in == space_id),
    )


async def proposals(session: AsyncSession, space_id: uuid.UUID) -> list[MemoryFact]:
    """What waits for the owner on this space's screen, oldest first."""
    q = select(MemoryFact).where(
        MemoryFact.status == PROPOSED, MemoryFact.valid_to.is_(None), _from_space(space_id)
    ).order_by(MemoryFact.created_at, MemoryFact.seq)
    return list((await session.execute(q)).scalars().all())


def vouched_at(fact: MemoryFact) -> datetime | None:
    """When something last stood behind the fact: the owner confirmed it, a source said it
    again, or it was learned."""
    stamps = [t for t in (fact.confirmed_at, fact.last_seen_at, fact.valid_from) if t]
    return max(stamps) if stamps else None


async def stale(
    session: AsyncSession, space_id: uuid.UUID, now: datetime | None = None
) -> list[MemoryFact]:
    """Live facts nothing has vouched for in `STALE_AFTER`. The editor's own text is not
    judged this way — it is the owner's standing word, re-read on every save."""
    cutoff = _now(now) - STALE_AFTER
    q = select(MemoryFact).where(
        MemoryFact.status == ACTIVE, MemoryFact.valid_to.is_(None),
        MemoryFact.evidence_kind != EV_PROFILE, _from_space(space_id),
    ).order_by(MemoryFact.valid_from, MemoryFact.seq)
    rows = (await session.execute(q)).scalars().all()
    return [f for f in rows if (vouched_at(f) or f.created_at) < cutoff]
