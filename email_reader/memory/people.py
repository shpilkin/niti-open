"""Who a person is: the profile the user's memory hangs on, and the identities that lead
to it.

A person reaches Niti under several names — an address in the mail, a name the model read
off a transcript, a name typed into a task. Each of those is a `PersonIdentity` row; the
`Person` row they point at is the profile, and the user merges rows into one profile when
they are the same human being (docs/feature-people.md).

Global across spaces by decision (docs/feature-memory.md §2.1): identity and what the user
knows about someone do not depend on which mailbox they wrote to. What the person *did* in
a space is `memory.stats`, scoped per space. Rows are created lazily on first open or
edit — no backfill.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.enums import Direction, IdentityKind
from email_reader.memory import facts
from email_reader.models import Email, MemoryFact, Person, PersonIdentity
from email_reader.normalization.identity import normalize_addr

# A name too short to be anybody in particular: «Ян» matches half the address book, and a
# wrong merge costs more than a missing row (see email_reader/people.py on token length).
NAME_MIN = 3
_SPACE_RE = re.compile(r"\s+", re.UNICODE)
# Diarization placeholders: a transcript says «Спикер 1» until it can name the voice.
_PLACEHOLDER_RE = re.compile(r"^(спикер|speaker|участник)\s*\d*$", re.IGNORECASE)


def canonical(addr: str) -> str:
    return normalize_addr(addr or "")


def name_key(name: str) -> str:
    """Comparison key for a name: lowercase, single spaces. Deliberately NOT fuzzy —
    «Аня» must never resolve to «Анна» on its own; that is what merging is for."""
    return _SPACE_RE.sub(" ", (name or "").strip()).lower()


def is_placeholder(name: str) -> bool:
    return bool(_PLACEHOLDER_RE.match((name or "").strip()))


def identity_of(text: str) -> tuple[str, str] | None:
    """(kind, value) for a counterparty string, or None when there is nothing to key on.

    Accepts what the boards actually store: an address, a name, or «Имя (addr)» — the
    address wins there, because it is the identity that can be verified."""
    text = (text or "").strip()
    if not text:
        return None
    m = _ADDR_IN_TEXT.search(text)
    if m:
        return IdentityKind.EMAIL.value, canonical(m.group(0))
    key = name_key(text)
    if len(key) < NAME_MIN or is_placeholder(key):
        return None
    return IdentityKind.NAME.value, key


_ADDR_IN_TEXT = re.compile(r"[^\s<>()\[\],;]+@[^\s<>()\[\],;]+")


def display_of(text: str) -> str:
    """What to show for a counterparty string: the name without the «(addr)» tail, else
    the text itself."""
    text = (text or "").strip()
    without = re.sub(r"\s*\(" + _ADDR_IN_TEXT.pattern + r"\)\s*$", "", text).strip()
    return without or text


async def get_identity(
    session: AsyncSession, kind: str, value: str
) -> PersonIdentity | None:
    if not value:
        return None
    return (
        await session.execute(
            select(PersonIdentity).where(
                PersonIdentity.kind == kind, PersonIdentity.value == value
            )
        )
    ).scalar_one_or_none()


async def identities_of(session: AsyncSession, person_id: uuid.UUID) -> list[PersonIdentity]:
    return list(
        (
            await session.execute(
                select(PersonIdentity)
                .where(PersonIdentity.person_id == person_id)
                .order_by(PersonIdentity.kind, PersonIdentity.value)
            )
        ).scalars().all()
    )


async def get_person(session: AsyncSession, addr: str) -> Person | None:
    """The profile that owns this address, if any."""
    addr = canonical(addr)
    if not addr:
        return None
    ident = await get_identity(session, IdentityKind.EMAIL.value, addr)
    if ident is not None:
        return await session.get(Person, ident.person_id)
    # Profiles that predate identities (or were written directly) still carry the address.
    return (
        await session.execute(select(Person).where(Person.email == addr))
    ).scalar_one_or_none()


async def get_person_by_name(session: AsyncSession, name: str) -> Person | None:
    key = name_key(name)
    if len(key) < NAME_MIN:
        return None
    ident = await get_identity(session, IdentityKind.NAME.value, key)
    return await session.get(Person, ident.person_id) if ident else None


async def resolve(session: AsyncSession, text: str) -> Person | None:
    """The profile for a counterparty string («Имя (addr)», an address, or a name)."""
    ident = identity_of(text)
    if ident is None:
        return None
    kind, value = ident
    row = await get_identity(session, kind, value)
    if row is not None:
        return await session.get(Person, row.person_id)
    return await get_person(session, value) if kind == IdentityKind.EMAIL.value else None


async def add_identity(
    session: AsyncSession, person: Person, kind: str, value: str,
    display: str | None = None, source: str | None = None,
) -> PersonIdentity | None:
    """Attach an identity to a profile. An identity that already belongs to somebody is
    left alone — one address is one person, and stealing it silently would merge two
    profiles behind the user's back."""
    if not value:
        return None
    existing = await get_identity(session, kind, value)
    if existing is not None:
        if existing.person_id == person.id and display and not existing.display:
            existing.display = display
        return existing if existing.person_id == person.id else None
    row = PersonIdentity(
        person_id=person.id, kind=kind, value=value, display=display, source=source
    )
    session.add(row)
    await session.flush()
    return row


async def ensure_person(
    session: AsyncSession, addr: str, name: str | None = None, source: str | None = None
) -> Person:
    """The profile for an address, created on first use."""
    addr = canonical(addr)
    if not addr:
        raise ValueError("empty address")
    person = await get_person(session, addr)
    if person is None:
        person = Person(email=addr, name=(name or "").strip() or None)
        session.add(person)
        await session.flush()
    elif name and not person.name:
        person.name = name.strip()
    if person.email is None:
        person.email = addr
    await add_identity(
        session, person, IdentityKind.EMAIL.value, addr, name or addr, source or "mail"
    )
    return person


async def ensure_person_by_name(
    session: AsyncSession, name: str, source: str | None = None
) -> Person | None:
    """The profile for somebody known only by name — from a transcript, a Telegram note or
    a typed task. None when the name is too short or a diarization placeholder."""
    display = display_of(name)
    key = name_key(display)
    if len(key) < NAME_MIN or is_placeholder(key):
        return None
    person = await get_person_by_name(session, key)
    if person is None:
        person = Person(name=display)
        session.add(person)
        await session.flush()
    await add_identity(session, person, IdentityKind.NAME.value, key, display, source)
    return person


async def ensure_for(
    session: AsyncSession, text: str, source: str | None = None
) -> Person | None:
    """The profile for any counterparty string, created if needed."""
    ident = identity_of(text)
    if ident is None:
        return None
    kind, value = ident
    if kind == IdentityKind.EMAIL.value:
        return await ensure_person(session, value, display_of(text) or None, source)
    return await ensure_person_by_name(session, text, source)


def display_name(person: Person, identities: list[PersonIdentity] | None = None) -> str:
    if person.name:
        return person.name
    for i in identities or []:
        if i.display:
            return i.display
    return person.email or "Без имени"


# --- Notes -------------------------------------------------------------------------


async def notes_of(session: AsyncSession, person: Person) -> str:
    """What the user knows about a person, as the text it was written as."""
    return await facts.notes_of(session, person.id)


async def set_notes(
    session: AsyncSession, person: Person, notes: str, now: datetime | None = None
) -> Person:
    """The pane's editor: every line is a fact, and a line that stayed keeps its row."""
    now = now or datetime.now(UTC)
    await facts.set_lines(
        session, subject_kind=facts.PERSON, subject_key=facts.person_key(person.id),
        text=notes, evidence_kind=facts.EV_NOTES, now=now,
    )
    person.updated_at = now
    return person


async def append_note(
    session: AsyncSession, person: Person, text: str, now: datetime | None = None
) -> MemoryFact:
    """«Запомнить»: one dated fact about the person (keeps what was there)."""
    now = now or datetime.now(UTC)
    fact = await facts.add(
        session, subject_kind=facts.PERSON, subject_key=facts.person_key(person.id),
        text=text, evidence_kind=facts.EV_REMEMBER, now=now,
    )
    person.updated_at = now
    return fact


# --- Merge -------------------------------------------------------------------------


def _parse_stamp(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


async def snapshot(session: AsyncSession, person: Person) -> dict:
    """Everything needed to put a profile back the way it was (undo payload)."""
    return {
        "id": str(person.id), "email": person.email, "name": person.name,
        "notes": await facts.notes_of(session, person.id), "is_me": person.is_me,
        # ISO, because the snapshot travels to the browser and back as JSON.
        "updated_at": person.updated_at.isoformat() if person.updated_at else None,
        "identities": [
            {"kind": i.kind, "value": i.value, "display": i.display, "source": i.source}
            for i in await identities_of(session, person.id)
        ],
    }


async def merge(
    session: AsyncSession, keep_id: uuid.UUID, drop_ids: list[uuid.UUID]
) -> tuple[Person | None, dict]:
    """Fold profiles into one: identities and memory facts re-point, the empty rows go.

    Returns the survivor and the undo payload — a snapshot of everyone absorbed, with the
    ids of the facts that moved, so «Отменить» can point them back. Nothing else in the
    schema points at `people.id`, so these rows are the whole of it."""
    keep = await session.get(Person, keep_id)
    if keep is None:
        return None, {}
    kept_before = await snapshot(session, keep)
    undo: list[dict] = []
    for drop_id in drop_ids:
        if drop_id == keep_id:
            continue
        drop = await session.get(Person, drop_id)
        if drop is None:
            continue
        snap = await snapshot(session, drop)
        snap["facts"] = await facts.move_person(session, from_id=drop.id, to_id=keep.id)
        undo.append(snap)
        keep.is_me = keep.is_me or drop.is_me
        if not keep.name and drop.name:
            keep.name = drop.name
        for ident in await identities_of(session, drop_id):
            ident.person_id = keep.id
        # The address is unique across profiles, so it has to be released before the
        # survivor can take it — two UPDATEs in one flush would collide on the constraint.
        carried, drop.email = drop.email, None
        await session.flush()
        if keep.email is None and carried:
            keep.email = carried
        await session.flush()
        await session.delete(drop)
    if undo:
        keep.updated_at = datetime.now(UTC)
    await session.flush()
    return keep, ({"keep": kept_before, "dropped": undo} if undo else {})


async def unmerge(session: AsyncSession, payload: dict) -> Person | None:
    """Undo a merge from its payload: every profile comes back with its own id, identities
    and memory facts — the survivor included, for its name and address.

    The address is unique, so the survivor lets go of everything first, the others take
    theirs back, and only then does the survivor get its own address again."""
    kept = payload.get("keep") or {}
    snapshots = payload.get("dropped") or []
    keep = await session.get(Person, uuid.UUID(kept["id"])) if kept.get("id") else None
    if keep is not None:
        keep.name = kept.get("name")
        keep.is_me = bool(kept.get("is_me"))
        keep.email = None
        await session.flush()
    for snap in snapshots:
        person = await session.get(Person, uuid.UUID(snap["id"]))
        if person is None:
            person = Person(
                id=uuid.UUID(snap["id"]), name=snap.get("name"), is_me=bool(snap.get("is_me")),
            )
            session.add(person)
            await session.flush()
        person.name = snap.get("name")
        person.updated_at = _parse_stamp(snap.get("updated_at"))
        await facts.restore_person(session, person_id=person.id, rows=snap.get("facts", []))
        for spec in snap.get("identities", []):
            row = await get_identity(session, spec["kind"], spec["value"])
            if row is not None:
                row.person_id = person.id
            else:
                session.add(PersonIdentity(
                    person_id=person.id, kind=spec["kind"], value=spec["value"],
                    display=spec.get("display"), source=spec.get("source"),
                ))
        await session.flush()
        person.email = snap.get("email")
        await session.flush()
    if keep is not None:
        keep.email = kept.get("email")
        keep.updated_at = _parse_stamp(kept.get("updated_at"))
        await session.flush()
    return keep


async def detach(
    session: AsyncSession, person_id: uuid.UUID, kind: str, value: str
) -> Person | None:
    """Pull one identity out of a profile into its own — the fix for a wrong merge noticed
    later, when the undo toast is long gone. The last identity cannot leave: that would be
    a rename, not a split."""
    row = await get_identity(session, kind, value)
    if row is None or row.person_id != person_id:
        return None
    idents = await identities_of(session, person_id)
    if len(idents) <= 1:
        return None
    person = await session.get(Person, person_id)
    fresh = Person(
        name=row.display or value,
        email=value if kind == IdentityKind.EMAIL.value else None,
    )
    session.add(fresh)
    await session.flush()
    row.person_id = fresh.id
    if person is not None and person.email == value:
        rest = [i for i in idents if i.value != value]
        person.email = next(
            (i.value for i in rest if i.kind == IdentityKind.EMAIL.value), None
        )
    await session.flush()
    return fresh


def resolve_address(q: str, emails: list[Email]) -> tuple[str | None, str | None]:
    """(address, display name) for a dossier query. A query that is an address wins; a name
    resolves to the most frequent inbound sender among the dossier's mail — the same rows
    the pane already shows, so what the user sees and what gets remembered agree."""
    q = (q or "").strip()
    if "@" in q:
        addr = canonical(q)
        name = next(
            (e.from_name for e in emails if canonical(e.from_addr) == addr and e.from_name),
            None,
        )
        return addr, name
    senders = Counter(
        canonical(e.from_addr) for e in emails
        if e.direction == Direction.INBOUND and e.from_addr
    )
    if not senders:
        return None, q or None
    addr = senders.most_common(1)[0][0]
    return addr, q or None
