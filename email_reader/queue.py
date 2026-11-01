"""DB-backed event queue (no external broker).

Producers enqueue rows into `events`; the worker claims them with
SELECT ... FOR UPDATE SKIP LOCKED, processes, and marks done/failed with backoff.

On SQLite (the desktop build) FOR UPDATE / SKIP LOCKED are silently dropped by
SQLAlchemy. That's fine: the desktop runs a single worker process, so there is
no concurrent claimer to lock against.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.enums import EventStatus
from email_reader.models import Event

# Event types whose handlers call the model — the only ones that can be "model unavailable".
MODEL_EVENT_TYPES = ("ThreadReady", "ProjectMatch", "MeetingReady")

MAX_ATTEMPTS = 5

# Give up on events parked (STALLED) longer than this. A transient outage recovers well
# within it; a *permanent* condition (revoked key, permanent 403/geo-block) would otherwise
# keep events waiting forever — so past the ceiling they become a real FAILED the user sees.
STALLED_MAX_WAIT = timedelta(hours=24)


async def enqueue(session: AsyncSession, type_: str, payload: dict) -> None:
    session.add(Event(type=str(type_), payload=payload, status=EventStatus.PENDING))


async def claim_one(session: AsyncSession) -> Event | None:
    """Claim a single due, pending event. Caller must commit after processing."""
    now = datetime.now(UTC)
    stmt = (
        select(Event)
        .where(Event.status == EventStatus.PENDING, Event.available_at <= now)
        .order_by(Event.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    event = (await session.execute(stmt)).scalar_one_or_none()
    if event is not None:
        event.status = EventStatus.PROCESSING
    return event


def mark_done(event: Event) -> None:
    event.status = EventStatus.DONE


def mark_retry_or_failed(event: Event, error: str, *, unavailable: bool = False) -> None:
    event.payload = {**event.payload, "last_error": error[:500]}
    if unavailable:
        # Model/infra down: don't spend the retry budget on an outage (that used to
        # hard-FAIL everything during a model 403). Park it; the worker's health-gate
        # revives STALLED events once the model is reachable again. available_at doubles
        # as the "parked since" clock that drives the give-up ceiling (fail_expired_stalled);
        # each genuine revive+re-park resets it, so only a persistently-dead model ages out.
        event.status = EventStatus.STALLED
        event.available_at = datetime.now(UTC)
        return
    event.attempts += 1
    if event.attempts >= MAX_ATTEMPTS:
        event.status = EventStatus.FAILED
    else:
        backoff = min(300, 2**event.attempts)
        event.status = EventStatus.PENDING
        event.available_at = datetime.now(UTC) + timedelta(seconds=backoff)


async def count_stalled(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Event).where(Event.status == EventStatus.STALLED)
        )
    ).scalar_one()


async def revive_stalled(session: AsyncSession, ids: list[int] | None = None) -> int:
    """Model is back: return parked (STALLED) events — all, or just `ids` — to the queue.
    Returns how many."""
    stmt = update(Event).where(Event.status == EventStatus.STALLED)
    if ids is not None:
        stmt = stmt.where(Event.id.in_(ids))
    res = await session.execute(
        stmt.values(status=EventStatus.PENDING, available_at=datetime.now(UTC))
    )
    return res.rowcount or 0


async def reclassify_failed_as_stalled(session: AsyncSession) -> int:
    """One-off recovery: move already-FAILED model events that died from a model/infra
    outage into STALLED, so the health-gate retries them when the model recovers. Before
    this feature an outage exhausted MAX_ATTEMPTS and hard-failed every queued event.

    An event with a newer one about the same subject stays failed: the newer one already
    did (or is doing) that work, and a revival would pay for it twice."""
    from email_reader.ai.errors import message_is_model_unavailable

    rows = (
        await session.execute(
            select(Event.id, Event.type, Event.status, Event.payload)
            .where(Event.type.in_(MODEL_EVENT_TYPES))
            .order_by(Event.id.desc())
        )
    ).all()
    seen: set[tuple[str, str]] = set()
    rescue: list[int] = []
    for ev_id, type_, status, payload in rows:
        payload = payload or {}
        about = {k: v for k, v in payload.items() if k != "last_error"}
        subject = (str(type_), json.dumps(about, sort_keys=True))
        superseded = bool(about) and subject in seen
        seen.add(subject)
        if (
            status == EventStatus.FAILED and not superseded
            and message_is_model_unavailable(payload.get("last_error", ""))
        ):
            rescue.append(ev_id)
    if rescue:
        await session.execute(
            update(Event).where(Event.id.in_(rescue)).values(
                status=EventStatus.STALLED, attempts=0,
                available_at=datetime.now(UTC),  # start the give-up clock at rescue time
            )
        )
    return len(rescue)


async def fail_expired_stalled(
    session: AsyncSession, max_wait: timedelta = STALLED_MAX_WAIT,
    ids: list[int] | None = None,
) -> int:
    """Stop waiting on events parked (STALLED) longer than max_wait: mark them FAILED.

    Parking assumes the model comes back; a permanent condition (revoked key, permanent
    403, geo-block) never recovers, so without a ceiling those events would wait forever
    and the health-gate would ping a dead endpoint indefinitely. Past the ceiling we
    surface them as real failures (visible in the bar, reprocessable by hand)."""
    cutoff = datetime.now(UTC) - max_wait
    stmt = select(Event).where(Event.status == EventStatus.STALLED, Event.available_at < cutoff)
    if ids is not None:
        stmt = stmt.where(Event.id.in_(ids))
    rows = (await session.execute(stmt)).scalars().all()
    hours = int(max_wait.total_seconds() // 3600)
    note = f"parked > {hours}h without model recovery — gave up"
    for ev in rows:
        prev = (ev.payload or {}).get("last_error", "")
        ev.payload = {**(ev.payload or {}), "last_error": f"{prev} | {note}" if prev else note}
        ev.status = EventStatus.FAILED
        ev.attempts = MAX_ATTEMPTS
    return len(rows)


async def reset_stuck_processing(session: AsyncSession, older_than_seconds: int = 600) -> None:
    """Recover events stuck in 'processing' after a crash."""
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    await session.execute(
        text(
            "UPDATE events SET status='pending' "
            "WHERE status='processing' AND available_at < :cutoff"
        ),
        {"cutoff": cutoff},
    )
