"""Event worker: claims events from the Postgres queue and dispatches handlers.

One event per transaction. Handlers are looked up lazily to avoid import cycles.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import astuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.db import session_scope
from email_reader.enums import EventStatus, EventType
from email_reader.model_connections import ResolvedModel
from email_reader.models import Account, ActionItem, Event, Meeting, Thread
from email_reader.queue import (
    STALLED_MAX_WAIT,
    claim_one,
    fail_expired_stalled,
    mark_done,
    mark_retry_or_failed,
    reclassify_failed_as_stalled,
    reset_stuck_processing,
    revive_stalled,
)

log = logging.getLogger("email_reader.worker")

# How often (seconds) the idle loop pings the model to see if parked events can resume.
HEALTH_CHECK_INTERVAL = 60.0

Handler = Callable[[AsyncSession, dict], Awaitable[None]]


def _handlers() -> dict[str, Handler]:
    # Imported here to avoid circular imports at module load.
    from email_reader.advice import compute as process_advice_needed
    from email_reader.commitment.engine import process_thread_ready
    from email_reader.meetings import process_meeting_ready
    from email_reader.memory.capture import process_facts_capture
    from email_reader.notify import process_followup_due
    from email_reader.pipeline.process import process_email_stored
    from email_reader.projects import process_project_match
    from email_reader.read_sync import process_mark_seen

    return {
        EventType.EMAIL_STORED: process_email_stored,
        EventType.THREAD_READY: process_thread_ready,
        EventType.FOLLOWUP_DUE: process_followup_due,
        EventType.PROJECT_MATCH: process_project_match,
        EventType.MARK_SEEN: process_mark_seen,
        EventType.MEETING_READY: process_meeting_ready,
        EventType.FACTS_CAPTURE: process_facts_capture,
        EventType.ADVICE_NEEDED: process_advice_needed,
    }


async def process_one() -> bool:
    """Process at most one event. Returns True if an event was handled.

    The claim and the handler run in SEPARATE transactions on purpose: a handler may
    block on a slow LLM call for seconds, and holding the PROCESSING write lock across
    it serialized every other writer — the web UI then failed with "database is locked"
    (e.g. saving a mailbox's model). Committing the claim first releases the lock so the
    handler only takes it briefly when it writes its results.
    """
    handlers = _handlers()
    # 1) Claim in a short, committed transaction.
    async with session_scope() as session:
        event = await claim_one(session)
        if event is None:
            return False
        event_id, etype, payload = event.id, str(event.type), dict(event.payload)

    # 2) Process in its own transaction; reload the row to record the outcome.
    handler = handlers.get(etype)
    async with session_scope() as session:
        if handler is None:
            log.warning("No handler for event type %s", etype)
            ev = await session.get(Event, event_id)
            if ev is not None:
                mark_done(ev)
            return True
        try:
            await handler(session, payload)
            ev = await session.get(Event, event_id)
            if ev is not None:
                mark_done(ev)
        except Exception as exc:  # noqa: BLE001
            from email_reader.ai.errors import is_model_unavailable

            unavailable = is_model_unavailable(exc)
            # A model outage is expected noise, not a bug — log it quietly so it doesn't
            # drown the logs, and park the event instead of burning its retry budget.
            if unavailable:
                log.warning("Event %s (%s) parked — model unavailable: %r", event_id, etype, exc)
            else:
                log.exception("Event %s (%s) failed", event_id, etype)
            await session.rollback()  # discard the handler's partial writes
            ev = await session.get(Event, event_id)
            if ev is not None:
                mark_retry_or_failed(ev, repr(exc), unavailable=unavailable)
        return True


# How often the idle worker refreshes the person-stats cache the boards order by.
STATS_REFRESH_INTERVAL = 900.0


async def _maybe_refresh_stats() -> None:
    """A bounded sweep over live items' counterparties whose sender history is missing
    or a day old — so the boards' memory order stays current without touching the render."""
    from email_reader.memory import stats as stats_svc

    async with session_scope() as session:
        n = await stats_svc.refresh_stale(session)
    if n:
        log.info("Sender history refreshed for %d counterpart(ies)", n)


# How often the idle worker reconciles the search index with the store. Writers call
# `search.index.index_*` directly; this is the net under a writer nobody wired up, and
# what fills the index the first time after the tables are created empty by a migration.
SEARCH_SWEEP_INTERVAL = 300.0


async def _maybe_sweep_search() -> None:
    """Keep the index in step with the store.

    An empty index means the tables were just created by a migration. It is filled right
    away, back to back, rather than 500 at a time by the sweep — search would stay
    half-blind for hours after an update — but in batches of their own transactions: one
    pass held the write lock for seconds, and everyone else's writes with
    it (docs/feature-storage-plan.md).
    """
    from email_reader.search import index as search_index

    async with session_scope() as session:
        fresh = not await search_index.counts(session)
    if fresh:
        added = await search_index.rebuild(session_scope)
        log.info("Search index built: %d document(s)", added)
        return

    async with session_scope() as session:
        changed = await search_index.reconcile(session)
    if changed["added"] or changed["removed"]:
        log.info("Search index: +%d / -%d document(s)", changed["added"], changed["removed"])


# Как часто простаивающий воркер добирает карточки, у которых совета ещё нет. По
# несколько за заход: вызов модели идёт в общей очереди с разбором писем, и заваливать
# её сотней советов разом — значит задержать разбор новой почты.
ADVICE_SWEEP_INTERVAL = 120.0
ADVICE_SWEEP_BATCH = 5


async def _maybe_sweep_advice() -> None:
    from email_reader import advice as advice_svc

    async with session_scope() as session:
        n = await advice_svc.sweep(session, limit=ADVICE_SWEEP_BATCH)
    if n:
        log.info("Advice queued for %d card(s)", n)


# How often the idle worker queues a memory-capture pass for the spaces that have it on
# (memory.capture). Between passes there is nothing to read: a few letters a day.
FACTS_SWEEP_INTERVAL = 6 * 3600.0


async def _maybe_sweep_facts() -> None:
    from email_reader.memory import capture

    async with session_scope() as session:
        n = await capture.sweep(session)
    if n:
        log.info("Memory capture queued for %d space(s)", n)


async def _model_of(session: AsyncSession, ev: Event, memo: dict) -> ResolvedModel:
    """The model this parked event's handler would call now — the one to ask whether it
    answers again. Mirrors each handler's own resolution; other types ask the default."""
    from email_reader import model_connections as mc
    from email_reader.meetings import routing_model

    payload = ev.payload or {}
    etype = str(ev.type)
    try:
        if etype == EventType.MEETING_READY:
            meeting = await session.get(Meeting, uuid.UUID(payload["meeting_id"]))
            key = ("meeting", meeting.space_id if meeting else None)
            if key not in memo:
                memo[key] = await routing_model(session, key[1])
            return memo[key]
        if etype == EventType.FACTS_CAPTURE:
            space_id = uuid.UUID(payload["space_id"])
        elif etype in (EventType.THREAD_READY, EventType.PROJECT_MATCH, EventType.ADVICE_NEEDED):
            if etype == EventType.ADVICE_NEEDED:
                item = await session.get(ActionItem, uuid.UUID(payload["action_item_id"]))
                thread_id = item.source_thread_id if item else None
            else:
                thread_id = uuid.UUID(payload["thread_id"])
            space_id = (
                await session.execute(
                    select(Account.space_id)
                    .join(Thread, Thread.account_id == Account.id)
                    .where(Thread.id == thread_id)
                )
            ).scalar_one_or_none()
        else:
            space_id = None
    except (KeyError, ValueError, TypeError):
        space_id = None
    key = ("space", space_id)
    if key not in memo:
        memo[key] = await mc.resolve_for_space(session, space_id)
    return memo[key]


async def _maybe_revive_stalled() -> None:
    """When there are parked (STALLED) events, ask each model they wait on whether it
    answers again, and return to the queue only the events whose model does.

    Asking only the default model woke everything as soon as it answered: work waiting
    on another space's provider — out of money, say — went straight back to the same
    refusal every minute and never aged out. The model is re-resolved on every check, so
    a space switched to another connection gets its parked work back once that answers."""
    from email_reader.ai import extractor

    groups: dict[tuple, tuple[ResolvedModel, list[int]]] = {}
    async with session_scope() as session:
        parked = (
            await session.execute(select(Event).where(Event.status == EventStatus.STALLED))
        ).scalars().all()
        memo: dict = {}
        for ev in parked:
            rm = await _model_of(session, ev, memo)
            groups.setdefault(astuple(rm), (rm, []))[1].append(ev.id)

    for rm, ids in groups.values():
        if not rm.api_key:
            continue  # no usable model to ping — leave these parked
        err = await extractor.test_connection(
            kind=rm.kind, base_url=rm.base_url, api_key=rm.api_key, model=rm.model,
            no_thinking=rm.no_thinking,
        )
        async with session_scope() as session:
            if err is None:
                n = await revive_stalled(session, ids)
                if n:
                    log.info("Model %s reachable again — revived %d parked event(s)",
                             rm.model, n)
                continue
            log.debug("Model %s still unavailable, keeping %d event(s) parked: %s",
                      rm.model, len(ids), err[:160])
            # Still down: give up on anything parked past the ceiling so a permanently-dead
            # endpoint surfaces as a real error instead of waiting (and pinging) forever.
            expired = await fail_expired_stalled(session, ids=ids)
        if expired:
            log.warning(
                "Gave up on %d event(s) parked > %dh without model %s recovering",
                expired, int(STALLED_MAX_WAIT.total_seconds() // 3600), rm.model,
            )


async def run_worker(idle_sleep: float = 1.0, stop: asyncio.Event | None = None) -> None:
    # Best-effort startup recovery. This runs concurrently with the initial sync (which
    # may hold a long write lock during the first bulk import); a transient lock here
    # must NOT crash the worker (and, via asyncio.gather, the whole backend). Stuck
    # events are re-claimable later regardless.
    try:
        async with session_scope() as session:
            await reset_stuck_processing(session)
            # Rescue events that a past model outage hard-failed before parking existed.
            revived = await reclassify_failed_as_stalled(session)
            if revived:
                log.info("Parked %d previously-failed model event(s) for retry", revived)
    except Exception:  # noqa: BLE001
        log.warning("startup recovery skipped (transient DB lock?)", exc_info=True)
    log.info("Worker started")
    last_health = 0.0
    last_stats = 0.0
    last_search = 0.0
    last_facts = 0.0
    last_advice = 0.0
    while stop is None or not stop.is_set():
        try:
            handled = await process_one()
        except Exception:  # noqa: BLE001
            log.exception("Worker loop error")
            handled = False
        if handled:
            continue
        # Idle: periodically check whether a parked-events model is back.
        now = time.monotonic()
        if now - last_health >= HEALTH_CHECK_INTERVAL:
            last_health = now
            try:
                await _maybe_revive_stalled()
            except Exception:  # noqa: BLE001 — health check must never crash the worker
                log.warning("stalled-event health check failed", exc_info=True)
        if now - last_stats >= STATS_REFRESH_INTERVAL:
            last_stats = now
            try:
                await _maybe_refresh_stats()
            except Exception:  # noqa: BLE001 — a cache sweep must never crash the worker
                log.warning("person-stats refresh failed", exc_info=True)
        if now - last_search >= SEARCH_SWEEP_INTERVAL:
            last_search = now
            try:
                await _maybe_sweep_search()
            except Exception:  # noqa: BLE001 — a sweep must never crash the worker
                log.warning("search index sweep failed", exc_info=True)
        if now - last_advice >= ADVICE_SWEEP_INTERVAL:
            last_advice = now
            try:
                await _maybe_sweep_advice()
            except Exception:  # noqa: BLE001 — a sweep must never crash the worker
                log.warning("advice sweep failed", exc_info=True)
        if now - last_facts >= FACTS_SWEEP_INTERVAL:
            last_facts = now
            try:
                await _maybe_sweep_facts()
            except Exception:  # noqa: BLE001 — a sweep must never crash the worker
                log.warning("memory capture sweep failed", exc_info=True)
        await asyncio.sleep(idle_sleep)
