"""Reading the index: what the user typed -> FTS5 expression -> hydrated results.

The query language is the one people already know from mail clients — `от:`/`from:`,
`кому:`/`to:`, `тема:`/`subject:`, `где:`/`in:`, `после:`/`after:`, `до:`/`before:` — and
the chips above the results are the same thing by another input: they append operators to
the line, so a filtered search stays a shareable string.

Two things happen outside FTS5. Dates and kinds are plain SQL on `search_docs`, where an
index helps. The space is applied while hydrating, against the source row: mailboxes and
meetings move between spaces, so a copy of the space inside the index would go stale.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from email_reader import clock, quickadd
from email_reader.enums import ActionStatus
from email_reader.models import (
    Account,
    ActionItem,
    CalendarEvent,
    Email,
    Meeting,
    MemoryFact,
    Person,
    Project,
)
from email_reader.search.index import KINDS, iso
from email_reader.search.text import normalize, stem, tokens

# Column weights for bm25: an exact hit in a title outranks a stem hit in a body. Order
# matches the FTS5 columns (title, body, people, title_stem, body_stem).
_WEIGHTS = (8.0, 1.0, 4.0, 3.0, 0.5)
# A year of age costs about as much as one extra body hit — recent mail wins ties.
_AGE_PER_YEAR = 1.0

_KIND_WORDS: dict[str, str] = {
    "письма": "mail", "письмо": "mail", "почта": "mail", "mail": "mail",
    "задачи": "card", "задача": "card", "обязательства": "card", "карточки": "card",
    "cards": "card",
    "встречи": "meeting", "встреча": "meeting", "meetings": "meeting",
    "моменты": "turn", "расшифровки": "turn", "turns": "turn",
    "календарь": "event", "события": "event", "calendar": "event",
    "люди": "person", "человек": "person", "people": "person",
    "проекты": "project", "проект": "project", "projects": "project",
    "нити": "project", "нить": "project", "threads": "project", "thread": "project",
    "память": "fact", "факты": "fact", "факт": "fact", "memory": "fact",
}

_FIELD_WORDS: dict[str, str] = {
    "from": "from", "от": "from",
    "to": "to", "кому": "to",
    "subject": "subject", "тема": "subject",
    "in": "kind", "где": "kind",
    "after": "after", "после": "after", "с": "after",
    "before": "before", "до": "before",
    "is": "is", "статус": "is",
    "has": "has", "есть": "has",
    "project": "project", "проект-": "project",
}

HL_ON, HL_OFF = "\x02", "\x03"

_OPERATOR = re.compile(r'(?P<key>[a-zA-Zа-яА-Я-]+):(?P<value>"[^"]*"|\S*)')


@dataclass(slots=True)
class Parsed:
    """A search line taken apart. `words` is what goes to FTS5 as free text."""

    words: list[str] = field(default_factory=list)
    subject: list[str] = field(default_factory=list)
    sender: list[str] = field(default_factory=list)
    recipient: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    after: date | None = None
    before: date | None = None
    status: str = ""
    has: str = ""
    project: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.words or self.subject or self.sender or self.recipient)

    @property
    def only_filters(self) -> bool:
        """Filters were given but no text: «all cards due before Friday» is a valid ask."""
        return self.is_empty and bool(
            self.kinds or self.after or self.before or self.status or self.project
        )


def _date_of(value: str, today: date) -> date | None:
    """A date the way the quick-add line takes it: «12.08», «вчера», «пн», «2026-08-12»."""
    value = value.strip().strip('"')
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    _, parsed = quickadd.parse(value, today)
    return parsed


def parse(line: str, *, today: date | None = None) -> Parsed:
    today = today or clock.today()
    out = Parsed()
    rest = line or ""
    for m in _OPERATOR.finditer(line or ""):
        key = _FIELD_WORDS.get(m.group("key").lower())
        if key is None:
            continue
        value = m.group("value").strip('"').strip()
        rest = rest.replace(m.group(0), " ", 1)
        if not value:
            continue
        if key == "subject":
            out.subject += tokens(value)
        elif key == "from":
            out.sender += tokens(value)
        elif key == "to":
            out.recipient += tokens(value)
        elif key == "kind":
            kind = _KIND_WORDS.get(normalize(value))
            if kind and kind not in out.kinds:
                out.kinds.append(kind)
        elif key == "after":
            out.after = _date_of(value, today)
        elif key == "before":
            out.before = _date_of(value, today)
        elif key == "is":
            out.status = normalize(value)
        elif key == "has":
            out.has = normalize(value)
        elif key == "project":
            out.project = value.strip()
    out.words = tokens(rest)
    return out


def _phrase(words: list[str], columns: str, *, prefix: bool) -> str:
    """One AND-group over a set of FTS5 columns."""
    if not words:
        return ""
    parts = [f'"{w}"' for w in words[:-1]] + [f'"{words[-1]}"{"*" if prefix else ""}']
    return f"{{{columns}}} : ({' AND '.join(parts)})"


def _stem_phrase(words: list[str], columns: str) -> str:
    """The same, against the stem columns — each word as its stem OR as typed.

    Snowball is not idempotent, and that asymmetry loses real hits: «чемодане» is
    indexed as «чемодан», but the base form «чемодан» stems further, to «чемода», and
    the two never meet. Roughly a fifth of the words in this mailbox stem differently on
    a second pass. Stemming to a fixed point would fix the pair and ruin the rest
    («событиях» collapses to «соб»), so the word as typed is simply looked for in the
    stem column too — it is what the base form usually is.
    """
    if not words:
        return ""
    groups = []
    for w in words:
        stemmed = stem(w)
        groups.append(f'("{w}" OR "{stemmed}")' if stemmed != w else f'"{w}"')
    return f"{{{columns}}} : ({' AND '.join(groups)})"


def match_expr(p: Parsed, *, prefix: bool = False) -> str:
    """The FTS5 MATCH string.

    Free words are looked for twice — as typed across title/body/people, and against the
    stem columns — joined by OR so a hit in either counts, while the bm25 weights keep
    the exact one on top. Stems are never given a `*`: a stem is already a truncation,
    and a prefix on top of it matches half the dictionary.
    """
    groups: list[str] = []
    if p.words:
        exact = _phrase(p.words, "title body people", prefix=prefix)
        stems = _stem_phrase(p.words, "title_stem body_stem")
        groups.append(f"({exact} OR {stems})")
    if p.subject:
        exact = _phrase(p.subject, "title", prefix=prefix)
        stems = _stem_phrase(p.subject, "title_stem")
        groups.append(f"({exact} OR {stems})")
    for who in (p.sender, p.recipient):
        if who:
            groups.append(_phrase(who, "people", prefix=prefix))
    return " AND ".join(groups)


def any_expr(words: list[str]) -> str:
    """OR over every word — as typed or as its stem, in any column.

    For relevance lookups («which facts touch this thread?»), where the query is a bag of
    words off a subject line and a few names: `match_expr` would demand all of them in one
    document. No prefix, for the reason given there.
    """
    groups = []
    for w in words:
        alts = sorted({w, stem(w)})
        groups.append("(" + " OR ".join(f'"{a}"' for a in alts) + ")")
    return " OR ".join(groups)


@dataclass(slots=True)
class Hit:
    kind: str
    ref: str
    at: datetime | None
    rank: float
    title_snip: str
    body_snip: str


# Кто умеет отвечать на уточнение. У момента встречи нет «кому», у карточки нет
# вложений: показывать их в ответ на «кому:» или «есть:вложение» — значит выдавать
# нефильтрованное за отфильтрованное, а это хуже пустого ответа.
_DIRECTED = ("mail", "card", "event")
_WITH_STATUS = ("card",)
_WITH_ATTACHMENT = ("mail",)


def kinds_for(p: Parsed) -> list[str]:
    """Виды, которые останутся в выдаче с учётом уточнений запроса."""
    kinds = p.kinds or list(KINDS)
    if p.sender or p.recipient:
        kinds = [k for k in kinds if k in _DIRECTED]
    if p.status:
        kinds = [k for k in kinds if k in _WITH_STATUS]
    if p.has:
        kinds = [k for k in kinds if k in _WITH_ATTACHMENT]
    return kinds


async def raw_hits(
    session: AsyncSession, p: Parsed, *, limit: int, prefix: bool = False,
    by_date: bool = False, any_word: bool = False,
) -> list[Hit]:
    """Ranked rows straight out of the index — no space scoping yet. `any_word` swaps
    the AND of the query language for the OR of a relevance lookup."""
    kinds = kinds_for(p)
    if not kinds:
        return []
    marks = {f"k{i}": k for i, k in enumerate(kinds)}
    where = [f"d.kind IN ({', '.join(':' + n for n in marks)})"]
    params: dict = dict(marks, lim=limit)
    expr = any_expr(p.words) if any_word else match_expr(p, prefix=prefix)
    if expr:
        where.append("search_index MATCH :m")
        params["m"] = expr
    if p.after:
        where.append("d.at >= :after")
        params["after"] = iso(datetime.combine(p.after, time.min, tzinfo=UTC))
    if p.before:
        where.append("d.at <= :before")
        params["before"] = iso(datetime.combine(p.before, time.max, tzinfo=UTC))

    weights = ", ".join(str(w) for w in _WEIGHTS)
    if expr:
        rank = (f"bm25(search_index, {weights}) + "
                f"(julianday('now') - julianday(d.at)) / 365.0 * {_AGE_PER_YEAR}")
        select_rank, order = rank, "ord ASC"
        # Control characters, not tags: the snippet carries other people's mail, so it
        # is escaped before rendering and only these two markers become <mark>.
        snips = (f"snippet(search_index, 0, '{HL_ON}', '{HL_OFF}', '…', 10) AS tsnip, "
                 f"snippet(search_index, 1, '{HL_ON}', '{HL_OFF}', '…', 14) AS bsnip")
    else:
        # Filters only: nothing to rank by, so it is a plain newest-first listing.
        select_rank, order = "0.0", "d.at DESC"
        snips = "'' AS tsnip, '' AS bsnip"
    if by_date and expr:
        order = "d.at DESC"

    sql = (
        f"SELECT d.kind, d.ref, d.at, {select_rank} AS ord, {snips} "
        "FROM search_index i JOIN search_docs d ON d.rowid = i.rowid "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY {order} LIMIT :lim"
    )
    rows = (await session.execute(text(sql), params)).all()
    return [Hit(kind=r[0], ref=r[1], at=_parse_at(r[2]), rank=float(r[3] or 0.0),
                title_snip=r[4] or "", body_snip=r[5] or "") for r in rows]


def _parse_at(value: str | None) -> datetime | None:
    """`search_docs.at` comes back as the text it was written as (see index.iso)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        return None


@dataclass(slots=True)
class Result:
    kind: str
    ref: str
    at: datetime | None
    title: str
    who: str
    snippet: str
    url: str
    pane: bool
    obj: object | None = None


def _url(kind: str, ref: str, obj, who: str = "") -> tuple[str, bool]:  # noqa: ANN001
    """Where a hit leads, and whether that URL fills the right-hand pane."""
    if kind == "fact":
        # A fact about a person opens that person; one about me opens the profile editor.
        if getattr(obj, "subject_kind", "") == "person" and who:
            return f"/person?q={who.split(' ')[-1]}", True
        return "/settings/section/profile", False
    if kind == "mail":
        return f"/email/{ref_uuid(ref)}", True
    if kind == "card":
        return f"/item/{ref_uuid(ref)}", True
    if kind == "meeting":
        return f"/meeting/{ref_uuid(ref)}", True
    if kind == "turn":
        mid, _, seconds = ref.partition(":")
        return f"/meeting/{ref_uuid(mid)}?at={_stamp(seconds)}", True
    if kind == "person":
        who = getattr(obj, "email", None) or getattr(obj, "name", "") or ""
        return f"/person?q={who}", True
    if kind == "project":
        return f"/project/{ref_uuid(ref)}", False
    if kind == "event":
        return f"/event/{ref_uuid(ref)}", True
    return "", False


def ref_uuid(ref: str) -> str:
    return str(uuid.UUID(hex=ref))


def _stamp(seconds: str) -> str:
    n = int(seconds or 0)
    return f"[{n // 3600:02d}:{n % 3600 // 60:02d}:{n % 60:02d}]"


async def _accounts_in_space(session: AsyncSession, space_id: uuid.UUID | None) -> set:
    if space_id is None:
        return set()
    return {
        a for a in (await session.execute(
            select(Account.id).where(Account.space_id == space_id)
        )).scalars()
    }


async def hydrate(
    session: AsyncSession, hits: list[Hit], *, space_id: uuid.UUID | None,
    per_kind: int | None = None,
) -> list[Result]:
    """Load the things behind the hits, drop what belongs to another space, keep order.

    Scoping happens here rather than in the index because a mailbox or a meeting can be
    moved to another space, and a space copied into the index would then be a lie.
    People are global by design (docs/feature-people.md) — they are never dropped.
    """
    by_kind: dict[str, list[Hit]] = {}
    for h in hits:
        by_kind.setdefault(h.kind, []).append(h)

    accounts = await _accounts_in_space(session, space_id)
    loaded: dict[tuple[str, str], object] = {}

    async def _load(model, ids: list[uuid.UUID], *cols) -> dict:  # noqa: ANN001
        if not ids:
            return {}
        stmt = select(model).where(model.id.in_(ids))
        if cols:
            stmt = stmt.options(load_only(*cols))
        return {o.id: o for o in (await session.execute(stmt)).scalars()}

    for kind, group in by_kind.items():
        refs = [uuid.UUID(hex=h.ref.split(":", 1)[0]) for h in group]
        if kind == "mail":
            rows = await _load(Email, refs, Email.subject, Email.from_name, Email.from_addr,
                               Email.sent_at, Email.account_id, Email.thread_id,
                               Email.has_attachments, Email.direction,
                               Email.to_addrs, Email.cc_addrs)
        elif kind == "card":
            rows = await _load(ActionItem, refs, ActionItem.title, ActionItem.type,
                               ActionItem.status, ActionItem.counterparty,
                               ActionItem.delegate_to, ActionItem.space_id,
                               ActionItem.due_date, ActionItem.created_at)
        elif kind in ("meeting", "turn"):
            rows = await _load(Meeting, refs, Meeting.title, Meeting.started_at,
                               Meeting.space_id, Meeting.deleted_at)
        elif kind == "event":
            rows = await _load(CalendarEvent, refs, CalendarEvent.title,
                               CalendarEvent.start_at, CalendarEvent.account_id,
                               CalendarEvent.organizer, CalendarEvent.attendees,
                               CalendarEvent.cancelled_at)
        elif kind == "person":
            rows = await _load(Person, refs, Person.name, Person.email)
        elif kind == "fact":
            rows = await _load(MemoryFact, refs, MemoryFact.text, MemoryFact.space_id,
                               MemoryFact.subject_kind, MemoryFact.subject_key,
                               MemoryFact.status, MemoryFact.valid_to,
                               MemoryFact.valid_from)
        else:
            rows = await _load(Project, refs, Project.name, Project.space_id)
        for h in group:
            obj = rows.get(uuid.UUID(hex=h.ref.split(":", 1)[0]))
            if obj is not None:
                loaded[(kind, h.ref)] = obj

    # A fact about a person is shown under that person's name.
    fact_who: dict[str, str] = {}
    person_keys = {
        o.subject_key for (k, _), o in loaded.items()
        if k == "fact" and o.subject_kind == "person" and o.subject_key
    }
    if person_keys:
        people = await _load(Person, [uuid.UUID(hex=k) for k in person_keys],
                             Person.name, Person.email)
        for (k, ref), o in loaded.items():
            if k == "fact" and o.subject_key:
                person = people.get(uuid.UUID(hex=o.subject_key))
                if person is not None:
                    fact_who[ref] = " ".join(
                        x for x in (person.name or "", person.email or "") if x
                    )

    out: list[Result] = []
    seen: dict[str, int] = {}
    for h in hits:
        obj = loaded.get((h.kind, h.ref))
        if obj is None or not _in_space(h.kind, obj, space_id, accounts):
            continue
        if per_kind is not None and seen.get(h.kind, 0) >= per_kind:
            continue
        seen[h.kind] = seen.get(h.kind, 0) + 1
        who = fact_who.get(h.ref, "") if h.kind == "fact" else _who_of(h.kind, obj)
        url, pane = _url(h.kind, h.ref, obj, who)
        out.append(Result(
            kind=h.kind, ref=h.ref, at=h.at,
            title=_title_of(h.kind, obj, h.title_snip), who=who,
            snippet=h.body_snip, url=url, pane=pane, obj=obj,
        ))
    return out


def _in_space(kind: str, obj, space_id: uuid.UUID | None, accounts: set) -> bool:  # noqa: ANN001
    if kind == "fact":
        # A retired fact may still sit in the index for a moment; it is not a result.
        if obj.status != "active" or obj.valid_to is not None:
            return False
        return space_id is None or obj.space_id is None or obj.space_id == space_id
    if space_id is None or kind == "person":
        return True
    if kind in ("mail", "event"):
        return obj.account_id in accounts
    if kind in ("meeting", "turn"):
        return obj.space_id == space_id and obj.deleted_at is None
    return obj.space_id == space_id


def _title_of(kind: str, obj, snip: str) -> str:  # noqa: ANN001
    if snip:
        return snip
    if kind == "mail":
        return obj.subject or "(без темы)"
    if kind == "person":
        return obj.name or obj.email or ""
    if kind == "fact":
        return obj.text
    return getattr(obj, "title", None) or getattr(obj, "name", "") or "(без названия)"


def _who_of(kind: str, obj) -> str:  # noqa: ANN001
    if kind == "mail":
        return obj.from_name or obj.from_addr or ""
    if kind == "card":
        return obj.counterparty or obj.delegate_to or ""
    if kind == "event":
        return obj.organizer or ""
    if kind == "person":
        return obj.email or ""
    return ""


def _addr_line(entries) -> str:  # noqa: ANN001
    return " ".join(f"{e.get('name', '')} {e.get('email', '')}"
                    for e in (entries or []) if isinstance(e, dict))


def _from_line(kind: str, obj) -> str:  # noqa: ANN001
    """«От кого» у каждого вида своё: отправитель письма, вторая сторона обязательства,
    организатор встречи в календаре."""
    if kind == "mail":
        return f"{obj.from_name or ''} {obj.from_addr or ''}"
    if kind == "card":
        return obj.counterparty or ""
    return obj.organizer or ""


def _to_line(kind: str, obj) -> str:  # noqa: ANN001
    """«Кому»: получатели письма вместе с копией — письмо, где человек в копии, адресовано
    и ему тоже; у обязательства — кому передано; у события — приглашённые."""
    if kind == "mail":
        return f"{_addr_line(obj.to_addrs)} {_addr_line(obj.cc_addrs)}"
    if kind == "card":
        return obj.delegate_to or ""
    return _addr_line(obj.attendees)


def _post_filter(p: Parsed, results: list[Result]) -> list[Result]:
    """Constraints FTS5 cannot express: direction of an address, a card's status, an
    attachment. Cheap because it runs on the page of hits, not on the store.

    Виды, которые про такое уточнение ничего не знают, сюда уже не доходят — их отсекает
    `kinds_for`.
    """
    out = results
    if p.sender:
        needle = " ".join(p.sender)
        out = [r for r in out if _mentions(_from_line(r.kind, r.obj), needle)]
    if p.recipient:
        needle = " ".join(p.recipient)
        out = [r for r in out if _mentions(_to_line(r.kind, r.obj), needle)]
    if p.status in ("open", "открыт", "открытые"):
        out = [r for r in out
               if r.obj.status in (ActionStatus.OPEN, ActionStatus.OVERDUE)]
    elif p.status in ("done", "закрыт", "закрытые"):
        out = [r for r in out if r.obj.status == ActionStatus.DONE]
    if p.has in ("attachment", "вложение", "вложения"):
        out = [r for r in out if r.obj.has_attachments]
    return out


def _mentions(haystack: str, needle: str) -> bool:
    hay = normalize(haystack)
    return all(w in hay for w in tokens(needle))


# How many raw hits to pull for one page of results. Space scoping and the post-filters
# both thin the list out, so the index is asked for several times what will be shown.
_OVERFETCH = 6


async def search(
    session: AsyncSession, line: str, *, space_id: uuid.UUID | None = None,
    limit: int = 60, per_kind: int | None = None, prefix: bool = False,
    by_date: bool = False, today: date | None = None,
) -> tuple[Parsed, list[Result]]:
    p = parse(line, today=today)
    if p.is_empty and not p.only_filters:
        return p, []
    hits = await raw_hits(session, p, limit=limit * _OVERFETCH, prefix=prefix,
                          by_date=by_date)
    results = await hydrate(session, hits, space_id=space_id, per_kind=None)
    results = _post_filter(p, results)
    if per_kind is not None:
        capped: list[Result] = []
        seen: dict[str, int] = {}
        for r in results:
            if seen.get(r.kind, 0) >= per_kind:
                continue
            seen[r.kind] = seen.get(r.kind, 0) + 1
            capped.append(r)
        results = capped
    return p, results[:limit]


def grouped(results: list[Result]) -> list[tuple[str, str, list[Result]]]:
    """Results by kind, in the order KINDS declares — (kind, plural label, rows)."""
    by_kind: dict[str, list[Result]] = {}
    for r in results:
        by_kind.setdefault(r.kind, []).append(r)
    return [(k, KINDS[k][1], by_kind[k]) for k in KINDS if k in by_kind]
