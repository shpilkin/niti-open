"""Deterministic facts about a correspondent, computed from the mailbox itself.

No LLM, no stored state: everything is a query over `emails`, so it is as fresh as the last
sync and can be recomputed at will. Scoped by space_id — the isolation boundary
(docs/spaces.md): all mailboxes of the space count together, nothing from another space.
None means every space and is only for callers that explicitly asked for that.

Definitions (windowed to WINDOW_DAYS unless said otherwise):
- inbound        — their messages to this mailbox; low-value (triage) and SELF excluded
- inbound_to_me  — of those, where I am a direct To-recipient (CC creates no obligation,
                   same rule as commitment.engine._thread_flags)
- replied        — inbound_to_me messages followed by a message of mine in the same thread
- reply latency  — per message of mine, time since the latest inbound_to_me before it in the
                   thread; median over the window
- unanswered     — threads whose latest inbound_to_me is older than STALE_DAYS and has no
                   message of mine after it (per thread, so a nag sequence counts once)

Not in v1: forwards as a delegation signal. A forward starts a new thread, a forward to
myself is SELF, an FYI to my boss is not delegation — too noisy for a fact. It will feed
rule *proposals* instead.
"""

from __future__ import annotations

import re
import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import String as sa_String
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from email_reader.enums import LIVE_STATUSES, Direction
from email_reader.identities import get_identity_set
from email_reader.models import ActionItem, Email, PersonStat
from email_reader.normalization.identity import normalize_addr
from email_reader.spaces import account_ids_of

WINDOW_DAYS = 90
STALE_DAYS = 3
# A cached row older than this is recomputed by the worker's idle sweep.
STALE_AFTER = timedelta(hours=24)
_ADDR_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")

# Bodies are never needed here; without this every pane open would materialize the HTML
# of every message in the window.
_LEAN = load_only(
    Email.id, Email.thread_id, Email.sent_at, Email.from_addr, Email.to_addrs, Email.cc_addrs
)


@dataclass
class PersonStats:
    email: str
    window_days: int = WINDOW_DAYS
    inbound: int = 0
    inbound_to_me: int = 0
    outbound: int = 0
    replied: int = 0
    unanswered: int = 0
    median_reply_hours: float | None = None
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    first_seen_at: datetime | None = None  # earliest inbound ever (not windowed)
    total_inbound: int = 0  # inbound ever (not windowed)

    @property
    def has_data(self) -> bool:
        return bool(self.inbound or self.outbound or self.total_inbound)


def _addr_in(addrs: list | None, addr: str) -> bool:
    for a in addrs or []:
        if normalize_addr((a or {}).get("email") or "") == addr:
            return True
    return False


def _me_in_to(email: Email, identities: set[str]) -> bool:
    for a in email.to_addrs or []:
        cand = normalize_addr((a or {}).get("email") or "")
        if cand and cand in identities:
            return True
    return False


async def person_stats(
    session: AsyncSession,
    addr: str,
    space_id: uuid.UUID | None,
    *,
    window_days: int = WINDOW_DAYS,
    now: datetime | None = None,
) -> PersonStats:
    addr = normalize_addr(addr)
    now = now or datetime.now(UTC)
    since = now - timedelta(days=window_days)
    stats = PersonStats(email=addr, window_days=window_days)
    if not addr:
        return stats
    identities = await get_identity_set(session)

    def scoped(q):
        if space_id is None:
            return q
        return q.where(Email.account_id.in_(account_ids_of(space_id)))

    # All-time facts about their inbound: count and first contact.
    ever_q = scoped(
        select(func.count(Email.id), func.min(Email.sent_at)).where(
            Email.direction == Direction.INBOUND,
            func.lower(Email.from_addr) == addr,
            Email.is_low_value.is_(False),
        )
    )
    total, first = (await session.execute(ever_q)).one()
    stats.total_inbound = int(total or 0)
    stats.first_seen_at = first

    inbound_q = scoped(
        select(Email).options(_LEAN).where(
            Email.direction == Direction.INBOUND,
            func.lower(Email.from_addr) == addr,
            Email.is_low_value.is_(False),
            Email.sent_at >= since,
        )
    ).order_by(Email.sent_at)
    inbound = [
        e for e in (await session.execute(inbound_q)).scalars().all()
        if normalize_addr(e.from_addr) == addr
    ]
    stats.inbound = len(inbound)
    if inbound:
        stats.last_inbound_at = inbound[-1].sent_at
    to_me = [e for e in inbound if _me_in_to(e, identities)]
    stats.inbound_to_me = len(to_me)

    # My messages in the window: to them directly (outbound count), and in their threads
    # (replies — a reply-all where they sit in CC is still my answer to their message).
    # The LIKE on the recipient JSON is a superset prefilter; the exact check below decides.
    like = f"%{addr}%"
    mine_conds = [
        func.lower(func.cast(Email.to_addrs, sa_String)).like(like),
        func.lower(func.cast(Email.cc_addrs, sa_String)).like(like),
    ]
    their_threads = [e.thread_id for e in inbound if e.thread_id is not None]
    if their_threads:
        mine_conds.append(Email.thread_id.in_(their_threads))
    mine_q = scoped(
        select(Email).options(_LEAN).where(
            Email.direction == Direction.OUTBOUND, Email.sent_at >= since, or_(*mine_conds)
        )
    ).order_by(Email.sent_at)
    mine = list((await session.execute(mine_q)).scalars().all())
    to_them = [e for e in mine if _addr_in(e.to_addrs, addr) or _addr_in(e.cc_addrs, addr)]
    stats.outbound = len(to_them)
    if to_them:
        stats.last_outbound_at = to_them[-1].sent_at

    by_thread: dict[uuid.UUID, list[Email]] = {}
    for e in mine:
        if e.thread_id is not None:
            by_thread.setdefault(e.thread_id, []).append(e)

    latencies: list[float] = []
    latest_to_me: dict[uuid.UUID, Email] = {}
    for e in to_me:
        if e.thread_id is None:
            continue
        later = [m for m in by_thread.get(e.thread_id, []) if m.sent_at > e.sent_at]
        if later:
            stats.replied += 1
        prev = latest_to_me.get(e.thread_id)
        if prev is None or e.sent_at > prev.sent_at:
            latest_to_me[e.thread_id] = e
    for tid, last in latest_to_me.items():
        later = [m for m in by_thread.get(tid, []) if m.sent_at > last.sent_at]
        if not later and last.sent_at < now - timedelta(days=STALE_DAYS):
            stats.unanswered += 1
    # Latency: each message of mine against the latest of their To-me messages before it.
    to_me_by_thread: dict[uuid.UUID, list[Email]] = {}
    for e in to_me:
        if e.thread_id is not None:
            to_me_by_thread.setdefault(e.thread_id, []).append(e)
    for tid, msgs in by_thread.items():
        theirs = to_me_by_thread.get(tid)
        if not theirs:
            continue
        for m in msgs:
            before = [t for t in theirs if t.sent_at < m.sent_at]
            if before:
                delta = m.sent_at - max(t.sent_at for t in before)
                latencies.append(delta.total_seconds() / 3600)
    if latencies:
        stats.median_reply_hours = statistics.median(latencies)
    return stats


def hours_phrase(h: float) -> str:
    if h < 1:
        return "меньше часа"
    if h < 48:
        return f"за {round(h)} ч"
    return f"за {round(h / 24)} дн."


def describe(s: PersonStats) -> list[str]:
    """Human sentences for the person pane. Deterministic — these are the facts the
    assistant will later cite, so the wording must not promise more than the numbers."""
    if not s.has_data:
        return [f"Переписки за {s.window_days} дней нет."]
    out: list[str] = []
    if s.inbound:
        line = f"Пишет мне: {s.inbound} за {s.window_days} дней"
        if s.inbound_to_me != s.inbound:
            line += f", лично мне — {s.inbound_to_me}"
    else:
        line = f"За {s.window_days} дней не писал(а)"
    if s.total_inbound and s.first_seen_at:
        line += f"; всего {s.total_inbound} с {s.first_seen_at:%d.%m.%Y}"
    out.append(line + ".")
    if s.inbound_to_me:
        line = f"Я отвечаю: {s.replied} из {s.inbound_to_me}"
        if s.median_reply_hours is not None:
            line += f", обычно {hours_phrase(s.median_reply_hours)}"
        out.append(line + ".")
    if s.unanswered:
        out.append(f"Ждут моего ответа: {s.unanswered} (старше {STALE_DAYS} дн.).")
    if s.outbound and not s.inbound_to_me:
        out.append(f"Я писал(а): {s.outbound} за {s.window_days} дней.")
    return out


# ---- Cache: the numbers a board can read without recomputing them per row --------------

_CACHED_FIELDS = (
    "window_days", "inbound", "inbound_to_me", "outbound", "replied", "unanswered",
    "median_reply_hours", "last_inbound_at", "last_outbound_at", "first_seen_at",
    "total_inbound",
)


def address_of(counterparty: str | None) -> str | None:
    """The address inside a stored counterparty («Name (addr)» or a bare address);
    None for a name-only counterparty — nothing to look up then."""
    m = _ADDR_RE.search(counterparty or "")
    return normalize_addr(m.group(0)) if m else None


def _from_row(row: PersonStat) -> PersonStats:
    return PersonStats(email=row.email, **{f: getattr(row, f) for f in _CACHED_FIELDS})


async def cached(
    session: AsyncSession, space_id: uuid.UUID | None, addrs: set[str]
) -> dict[str, PersonStats]:
    """What the cache holds for these addresses in the space — possibly nothing yet."""
    if space_id is None or not addrs:
        return {}
    rows = (
        await session.execute(
            select(PersonStat).where(
                PersonStat.space_id == space_id, PersonStat.email.in_(sorted(addrs))
            )
        )
    ).scalars().all()
    return {r.email: _from_row(r) for r in rows}


async def refresh(
    session: AsyncSession, addr: str, space_id: uuid.UUID | None, *,
    now: datetime | None = None,
) -> PersonStats:
    """Compute and cache; the fresh numbers are returned either way."""
    now = now or datetime.now(UTC)
    st = await person_stats(session, addr, space_id, now=now)
    if space_id is None or not st.email:
        return st
    row = await session.get(PersonStat, (space_id, st.email))
    if row is None:
        row = PersonStat(space_id=space_id, email=st.email)
        session.add(row)
    for f in _CACHED_FIELDS:
        setattr(row, f, getattr(st, f))
    row.computed_at = now
    return st


async def refresh_stale(
    session: AsyncSession, *, limit: int = 25, now: datetime | None = None
) -> int:
    """Counterparties of live items whose cached numbers are missing or older than
    STALE_AFTER — at most `limit` recomputed per call, so the sweep never hogs the worker.
    Returns how many were refreshed."""
    now = now or datetime.now(UTC)
    rows = await session.execute(
        select(ActionItem.space_id, ActionItem.counterparty)
        .where(
            ActionItem.status.in_([s.value for s in LIVE_STATUSES]),
            ActionItem.counterparty.isnot(None),
        )
        .distinct()
    )
    wanted: set[tuple[uuid.UUID, str]] = set()
    for space_id, cp in rows:
        addr = address_of(cp)
        if addr and space_id is not None:
            wanted.add((space_id, addr))
    if not wanted:
        return 0
    fresh = {
        (r.space_id, r.email)
        for r in (
            await session.execute(
                select(PersonStat).where(PersonStat.computed_at >= now - STALE_AFTER)
            )
        ).scalars().all()
    }
    stale = sorted(wanted - fresh)[:limit]
    for space_id, addr in stale:
        await refresh(session, addr, space_id, now=now)
    return len(stale)


def memory_score(st: PersonStats | None, ongoing: bool) -> float:
    """The «только история» order from memory.backtest, the one that beat the VIP-first
    board on the user's own archive: is the thread already a conversation of mine, how often
    and how fast I answer this person. Someone the space has no numbers for is neutral."""
    rate, fast = 0.5, 0.0
    if st is not None and st.inbound_to_me:
        rate = st.replied / st.inbound_to_me
        fast = 1.0 if st.median_reply_hours is not None and st.median_reply_hours <= 24 else 0.0
    return 2.0 * (1.0 if ongoing else 0.0) + 2.0 * rate + 1.0 * fast
