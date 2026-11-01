"""The people of a space: one list assembled from every place a human being shows up.

There is no «contacts» table to read — a person is whoever the mail, the meetings and the
cards say exists. So the list is derived on every open from three cheap sources (cards,
`person_stats`, meeting participants), grouped by the identities they resolve to. Profiles
are created lazily — writing a note or merging is what turns a row into a `Person`
(docs/feature-people.md §3).

Scoped to the open space: the list is built from that space's own sources and nothing else
(docs/spaces.md §1). The profile behind a row is global — that is where the notes live.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.enums import ActionStatus, Direction, IdentityKind, SourceKind
from email_reader.identities import get_identity_set
from email_reader.memory import facts
from email_reader.memory import people as people_svc
from email_reader.models import (
    Account,
    ActionItem,
    Email,
    Meeting,
    Person,
    PersonIdentity,
    PersonStat,
)

# Snoozed included, like the dossier: deferred is still outstanding with this person.
LIVE = (
    ActionStatus.OPEN.value, ActionStatus.IN_PROGRESS.value, ActionStatus.OVERDUE.value,
    ActionStatus.SNOOZED.value,
)
# Enough for a real address book; the search box is what finds the rest.
LIST_LIMIT = 300
SEARCH_LIMIT = 50


@dataclass
class PersonRow:
    """One line of «Люди». `key` addresses it in the UI: a profile by id, a person nobody
    has merged or annotated yet by the identity they were seen under."""

    key: str
    name: str
    person_id: uuid.UUID | None = None
    identities: list[tuple[str, str, str | None]] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)
    open_items: int = 0
    total_items: int = 0
    last_at: datetime | None = None
    has_notes: bool = False

    @property
    def emails(self) -> list[str]:
        return [v for k, v, _ in self.identities if k == IdentityKind.EMAIL.value]

    @property
    def name_only(self) -> bool:
        return not self.emails


def identity_key(kind: str, value: str) -> str:
    return f"{kind}:{value}"


@dataclass
class _Cand:
    kind: str
    value: str
    display: str | None = None
    sources: set[str] = field(default_factory=set)
    open_items: int = 0
    total_items: int = 0
    last_at: datetime | None = None

    def seen(self, at: datetime | None) -> None:
        if at is not None and (self.last_at is None or at > self.last_at):
            self.last_at = at


def _put(cands: dict[tuple[str, str], _Cand], text: str, source: str) -> _Cand | None:
    ident = people_svc.identity_of(text)
    if ident is None:
        return None
    kind, value = ident
    cand = cands.get((kind, value))
    if cand is None:
        cand = _Cand(kind=kind, value=value)
        cands[(kind, value)] = cand
    if not cand.display:
        cand.display = people_svc.display_of(text) or None
    cand.sources.add(source)
    return cand


async def _from_items(session: AsyncSession, space_id: uuid.UUID, cands: dict) -> None:
    rows = await session.execute(
        select(
            ActionItem.counterparty, ActionItem.status, ActionItem.source_kind,
            ActionItem.created_at,
        ).where(ActionItem.space_id == space_id, ActionItem.counterparty.is_not(None))
    )
    for counterparty, status, source_kind, created_at in rows:
        cand = _put(cands, counterparty or "", source_kind or SourceKind.MAIL.value)
        if cand is None:
            continue
        cand.total_items += 1
        if status in LIVE:
            cand.open_items += 1
        cand.seen(created_at)


async def _from_stats(session: AsyncSession, space_id: uuid.UUID, cands: dict) -> None:
    rows = await session.execute(
        select(PersonStat.email, PersonStat.last_inbound_at, PersonStat.last_outbound_at)
        .where(PersonStat.space_id == space_id)
    )
    for email, last_in, last_out in rows:
        cand = _put(cands, email or "", SourceKind.MAIL.value)
        if cand is None:
            continue
        cand.seen(last_in)
        cand.seen(last_out)


async def _from_meetings(session: AsyncSession, space_id: uuid.UUID, cands: dict) -> None:
    rows = await session.execute(
        select(Meeting.participants, Meeting.started_at).where(
            Meeting.space_id == space_id, Meeting.deleted_at.is_(None)
        )
    )
    for participants, started_at in rows:
        for who in participants or []:
            cand = _put(cands, str(who), SourceKind.MEETING.value)
            if cand is not None:
                cand.seen(started_at)


async def _from_mail_search(
    session: AsyncSession, space_id: uuid.UUID, q: str, cands: dict
) -> None:
    """Senders the space knows but never made a card about — reachable by search only, so
    the list stays «people I deal with» instead of every newsletter ever received."""
    like = f"%{q}%"
    rows = await session.execute(
        select(Email.from_addr, Email.from_name, Email.sent_at)
        .where(
            Email.direction == Direction.INBOUND,
            Email.account_id.in_(select(Account.id).where(Account.space_id == space_id)),
            or_(Email.from_name.ilike(like), Email.from_addr.ilike(like)),
        )
        .order_by(Email.sent_at.desc())
        .limit(SEARCH_LIMIT * 4)
    )
    for from_addr, from_name, sent_at in rows:
        if not from_addr:
            continue
        text = f"{from_name} ({from_addr})" if from_name else from_addr
        cand = _put(cands, text, SourceKind.MAIL.value)
        if cand is not None:
            cand.seen(sent_at)


def _matches(row: PersonRow, q: str) -> bool:
    if not q:
        return True
    q = q.lower()
    if q in row.name.lower():
        return True
    return any(q in v or q in (d or "").lower() for _, v, d in row.identities)


async def list_people(
    session: AsyncSession, space_id: uuid.UUID, q: str = "", limit: int = LIST_LIMIT
) -> list[PersonRow]:
    """Everyone this space has seen, newest activity first."""
    q = (q or "").strip()
    cands: dict[tuple[str, str], _Cand] = {}
    await _from_items(session, space_id, cands)
    await _from_stats(session, space_id, cands)
    await _from_meetings(session, space_id, cands)
    if q:
        await _from_mail_search(session, space_id, q, cands)

    mine = await get_identity_set(session)
    cands = {
        k: c for k, c in cands.items()
        if not (c.kind == IdentityKind.EMAIL.value and c.value in mine)
    }
    if not cands:
        return []

    # Which of them already belong to a profile.
    idents = list(
        (
            await session.execute(
                select(PersonIdentity).where(
                    PersonIdentity.value.in_({c.value for c in cands.values()})
                )
            )
        ).scalars().all()
    )
    owner = {(i.kind, i.value): i.person_id for i in idents}
    person_ids = set(owner.values())
    people = {
        p.id: p for p in (
            await session.execute(select(Person).where(Person.id.in_(person_ids)))
        ).scalars().all()
    } if person_ids else {}
    noted = await facts.people_with_notes(session, people) if people else set()
    # A profile shows all of its identities, including the ones idle in this space: the
    # chips are «who this person is», which is global (docs/feature-memory.md §2.1).
    all_idents: dict[uuid.UUID, list[PersonIdentity]] = {}
    if person_ids:
        for row in (
            await session.execute(
                select(PersonIdentity).where(PersonIdentity.person_id.in_(person_ids))
            )
        ).scalars().all():
            all_idents.setdefault(row.person_id, []).append(row)

    rows: dict[str, PersonRow] = {}
    for (kind, value), cand in cands.items():
        pid = owner.get((kind, value))
        person = people.get(pid) if pid else None
        if person is not None and person.is_me:
            continue
        key = f"person:{pid}" if person is not None else identity_key(kind, value)
        row = rows.get(key)
        if row is None:
            if person is not None:
                idl = sorted(all_idents.get(person.id, []), key=lambda i: (i.kind, i.value))
                row = PersonRow(
                    key=key, person_id=person.id,
                    name=people_svc.display_name(person, idl),
                    identities=[(i.kind, i.value, i.display) for i in idl],
                    has_notes=person.id in noted,
                )
            else:
                row = PersonRow(
                    key=key, name=cand.display or value,
                    identities=[(kind, value, cand.display)],
                )
            rows[key] = row
        row.sources |= cand.sources
        row.open_items += cand.open_items
        row.total_items += cand.total_items
        if cand.last_at and (row.last_at is None or cand.last_at > row.last_at):
            row.last_at = cand.last_at

    out = [r for r in rows.values() if _matches(r, q)]
    out.sort(key=lambda r: (r.last_at is None, -(r.last_at.timestamp() if r.last_at else 0),
                            r.name.lower()))
    return out[:limit]


async def row_for_key(
    session: AsyncSession, space_id: uuid.UUID, key: str
) -> PersonRow | None:
    """The list row a key points at — used by the pane so it shows the same numbers."""
    for row in await list_people(session, space_id, limit=10_000):
        if row.key == key:
            return row
    return None


def identities_for(row: PersonRow) -> tuple[list[str], list[str]]:
    """(addresses, names) of a row — what the dossier looks the person up by, so a merged
    profile shows the mail of one address and the obligations named after another."""
    addresses = [v for k, v, _ in row.identities if k == IdentityKind.EMAIL.value]
    names = [
        (d or v) for k, v, d in row.identities if k == IdentityKind.NAME.value
    ]
    if row.name and not names and not addresses:
        names = [row.name]
    return addresses, names
