"""Backtest: does what the mailbox remembers predict what I actually act on?

The label is free (the Gmail Priority Inbox idea, Aberdeen et al. 2010): an inbound message
addressed to me counts as «acted on» when I wrote in its thread within WINDOW hours. Nobody
annotates anything. Features come only from what was known BEFORE the message arrived — no
leakage: the VIP list, my history with the sender (how often they wrote, how often and how
fast I answered), whether the thread was already a conversation of mine, whether I am the
only recipient, whether the message asks something.

Output: ranking quality (AUC — the chance that a random acted-on message ranks above a
random ignored one) of the board's current order (VIP first, i.e. one bit) against a score
that also uses the sender history, plus each feature alone — so the decision to change
views._priority_key rests on the user's own archive, not on taste.

    python -m email_reader.memory.backtest [--days 180] [--window 24] [--holdout]
"""

from __future__ import annotations

import asyncio
import bisect
import statistics
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import vip
from email_reader.enums import Direction
from email_reader.models import Email
from email_reader.normalization.identity import normalize_addr
from email_reader.spaces import account_ids_of

HISTORY_DAYS = 90
FEATURES = ("vip", "ongoing", "direct", "question", "familiar", "reply_rate", "fast")

# The candidate board order: VIP first, then what the history says. Weights are a starting
# point to be judged by the AUC below, not a model.
WEIGHTS = {
    "vip": 3.0, "ongoing": 2.0, "reply_rate": 2.0, "direct": 1.5, "fast": 1.0,
    "familiar": 0.5, "question": 0.5,
}


@dataclass
class Sample:
    email_id: uuid.UUID
    sent_at: datetime
    addr: str
    label: bool
    features: dict[str, float] = field(default_factory=dict)


def _addrs(raw: list | None) -> list[str]:
    return [normalize_addr((a or {}).get("email") or "") for a in raw or []]


def build_samples(
    rows, identities: set[str], vip_entries: list[dict], *, now: datetime, window_hours: int,
    history_days: int = HISTORY_DAYS,
) -> list[Sample]:
    """rows: objects with id, thread_id, direction, from_addr, to_addrs, cc_addrs, sent_at,
    is_low_value, question — every message of the space in the period, any order."""
    rows = sorted(rows, key=lambda r: r.sent_at)
    window = timedelta(hours=window_hours)
    history = timedelta(days=history_days)

    mine_by_thread: dict[uuid.UUID, list[datetime]] = defaultdict(list)
    for r in rows:
        if r.direction == Direction.OUTBOUND and r.thread_id is not None:
            mine_by_thread[r.thread_id].append(r.sent_at)

    def reply_after(thread_id, t: datetime) -> datetime | None:
        outs = mine_by_thread.get(thread_id, [])
        i = bisect.bisect_right(outs, t)
        return outs[i] if i < len(outs) else None

    # Every message they sent to me, with when (if ever) I answered it — the history a
    # later sample may look back on, but only the part that had happened by then.
    events: dict[str, list[tuple[datetime, datetime | None]]] = defaultdict(list)
    candidates = []
    for r in rows:
        if r.direction != Direction.INBOUND or r.is_low_value or r.thread_id is None:
            continue
        to = _addrs(r.to_addrs)
        if not any(a in identities for a in to):
            continue
        addr = normalize_addr(r.from_addr or "")
        if not addr or addr in identities:
            continue
        reply_at = reply_after(r.thread_id, r.sent_at)
        events[addr].append((r.sent_at, reply_at))
        candidates.append((r, addr, to, reply_at))

    samples: list[Sample] = []
    for r, addr, to, reply_at in candidates:
        if r.sent_at > now - window:
            continue  # the label window has not closed yet
        t = r.sent_at
        prior = [
            (s, rp) for s, rp in events[addr] if t - history <= s < t
        ]
        answered = [(s, rp) for s, rp in prior if rp is not None and rp < t]
        latencies = [(rp - s).total_seconds() / 3600 for s, rp in answered]
        externals = [a for a in to + _addrs(r.cc_addrs) if a and a not in identities]
        outs = mine_by_thread.get(r.thread_id, [])
        samples.append(
            Sample(
                email_id=r.id, sent_at=t, addr=addr,
                label=reply_at is not None and reply_at - t <= window,
                features={
                    "vip": 1.0 if vip.is_vip(addr, vip_entries) else 0.0,
                    "ongoing": 1.0 if bisect.bisect_left(outs, t) > 0 else 0.0,
                    "direct": 1.0 if not externals else 0.0,
                    "question": 1.0 if getattr(r, "question", False) else 0.0,
                    "familiar": min(len(prior), 10) / 10,
                    "reply_rate": len(answered) / len(prior) if prior else 0.5,
                    "fast": 1.0 if latencies and statistics.median(latencies) <= 24 else 0.0,
                },
            )
        )
    return samples


def score_baseline(s: Sample) -> float:
    return s.features["vip"]


def score_memory(s: Sample) -> float:
    return sum(WEIGHTS[f] * s.features[f] for f in WEIGHTS)


# Only the sender history — what a board could use without a VIP list at all.
HISTORY_WEIGHTS = {"ongoing": 2.0, "reply_rate": 2.0, "fast": 1.0}


def score_history(s: Sample) -> float:
    return sum(HISTORY_WEIGHTS[f] * s.features[f] for f in HISTORY_WEIGHTS)


def auc(scores: list[float], labels: list[bool]) -> float | None:
    """Mann-Whitney AUC with average ranks for ties; None without both classes."""
    pos = sum(1 for lb in labels if lb)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1  # ranks are 1-based
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    rank_sum = sum(r for r, lb in zip(ranks, labels, strict=True) if lb)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def precision_at(scores: list[float], labels: list[bool], share: float = 0.2) -> float | None:
    if not scores:
        return None
    k = max(1, round(len(scores) * share))
    top = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
    return sum(1 for i in top if labels[i]) / k


def report(samples: list[Sample], window_hours: int) -> str:
    if not samples:
        return "Нет входящих писем мне за период — сравнивать нечего."
    labels = [s.label for s in samples]
    pos = sum(labels)
    lines = [
        f"Писем мне: {len(samples)}, из них отвечено за {window_hours} ч: {pos} "
        f"({pos / len(samples):.0%}). Случайный порядок: AUC 0.500, точность топ-20% "
        f"{pos / len(samples):.0%}.",
        "",
        f"{'порядок':<28}{'AUC':>7}{'точность топ-20%':>20}",
    ]

    def row(name: str, scores: list[float]) -> str:
        a = auc(scores, labels)
        p = precision_at(scores, labels)
        a_txt = f"{a:.3f}" if a is not None else "—"
        p_txt = f"{p:.0%}" if p is not None else "—"
        return f"{name:<28}{a_txt:>7}{p_txt:>20}"

    lines.append(row("доска сегодня (VIP)", [score_baseline(s) for s in samples]))
    lines.append(row("VIP + история", [score_memory(s) for s in samples]))
    lines.append(row("только история", [score_history(s) for s in samples]))
    lines.append("")
    lines.append("по одному признаку:")
    for f in FEATURES:
        lines.append(row(f"  {f}", [s.features[f] for s in samples]))
    return "\n".join(lines)


CANDIDATES = {
    "доска сегодня (VIP)": score_baseline,
    "VIP + история": score_memory,
    "только история": score_history,
}


def holdout_report(samples: list[Sample], window_hours: int) -> str:
    """Pick the order on the first half of the period, judge it on the second — the
    in-sample numbers above flatter whatever was tuned on them."""
    if len(samples) < 40:
        return "Холдаут: слишком мало писем, чтобы делить период пополам."
    ordered = sorted(samples, key=lambda s: s.sent_at)
    mid = len(ordered) // 2
    train, test = ordered[:mid], ordered[mid:]
    train_labels = [s.label for s in train]
    test_labels = [s.label for s in test]
    train_auc = {
        name: auc([fn(s) for s in train], train_labels) for name, fn in CANDIDATES.items()
    }
    if any(v is None for v in train_auc.values()) or auc(
        [score_baseline(s) for s in test], test_labels
    ) is None:
        return "Холдаут: в одной из половин нет ни одного ответа — сравнивать нечего."
    best = max(train_auc, key=lambda k: train_auc[k])
    lines = [
        f"Холдаут: выбор на первой половине ({len(train)} писем), проверка на второй "
        f"({len(test)} писем, отвечено {sum(test_labels)}).",
        f"{'порядок':<28}{'AUC (1-я)':>10}{'AUC (2-я)':>10}",
    ]
    for name, fn in CANDIDATES.items():
        a_test = auc([fn(s) for s in test], test_labels)
        mark = "  ← выбран на первой половине" if name == best else ""
        lines.append(f"{name:<28}{train_auc[name]:>10.3f}{a_test:>10.3f}{mark}")
    return "\n".join(lines)


async def load_rows(session: AsyncSession, space_id: uuid.UUID, since: datetime):
    q = (
        select(
            Email.id, Email.thread_id, Email.direction, Email.from_addr, Email.to_addrs,
            Email.cc_addrs, Email.sent_at, Email.is_low_value,
            or_(Email.subject.contains("?"), Email.body_text.contains("?")).label("question"),
        )
        .where(
            Email.sent_at >= since,
            Email.direction != Direction.SELF,
            Email.account_id.in_(account_ids_of(space_id)),
        )
    )
    return list((await session.execute(q)).all())


async def run_space(
    session: AsyncSession, space_id: uuid.UUID, *, days: int, window_hours: int,
    now: datetime | None = None, holdout: bool = False,
) -> str:
    from email_reader.identities import get_identity_set

    now = now or datetime.now(UTC)
    rows = await load_rows(session, space_id, now - timedelta(days=days + HISTORY_DAYS))
    samples = build_samples(
        rows, await get_identity_set(session), await vip.get_entries(session),
        now=now, window_hours=window_hours,
    )
    samples = [s for s in samples if s.sent_at >= now - timedelta(days=days)]
    text = report(samples, window_hours)
    if holdout:
        text += "\n\n" + holdout_report(samples, window_hours)
    return text


async def main() -> None:
    from email_reader.db import session_scope
    from email_reader.spaces import list_spaces

    argv = sys.argv[1:]

    def opt(name: str, default: int) -> int:
        if name in argv:
            return int(argv[argv.index(name) + 1])
        return default

    days, window = opt("--days", 180), opt("--window", 24)
    holdout = "--holdout" in argv
    async with session_scope() as session:
        for space in await list_spaces(session):
            print(f"== {space.name} — последние {days} дн., окно ответа {window} ч ==")
            print(await run_space(
                session, space.id, days=days, window_hours=window, holdout=holdout,
            ))
            print()


if __name__ == "__main__":
    asyncio.run(main())
