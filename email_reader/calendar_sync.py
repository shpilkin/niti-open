"""Calendar sync: fetch (CalDAV) -> store CalendarEvent. Scoped by account_id.

Storage upserts on (account_id, provider_uid), so re-syncs are idempotent; events that
disappear from a COMPLETE fetch are marked cancelled. A fetch failure is recorded and
does not abort the rest.

The prep gate this used to drive is gone (the «К подготовке» board was removed): events
are stored, nothing is derived from them. Kept because the table is still the source of
mailbox attribution for the prep items already in the archive.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import settings_store
from email_reader.connectors import caldav
from email_reader.db import session_scope
from email_reader.models import Account, CalendarEvent
from email_reader.search import index as search_index

log = logging.getLogger("email_reader.calendar_sync")

# How far ahead to fetch. Неделя вперёд хватала, пока календарь только читали брифинг и
# поиск; с экраном «Месяц» дальше недели было пусто — за этим горизонтом оказались
# сотни встреч, которых Niti не видела вовсе. Девяносто дней — месяц в сетке и запас.
DEFAULT_HORIZON_DAYS = 90

# Календарь события запоминаем с 24.09.2026; прошедшие встречи сверка вперёд не трогает,
# поэтому один раз на ящик дотягиваем их календари за этот отрезок назад.
NAMES_BACKFILL_DAYS = 90
_NAMES_FILLED = "calendar.names_filled.{}"

async def store_events(
    session: AsyncSession, account_id: uuid.UUID, fetched: list[caldav.FetchedEvent]
) -> dict:
    """Upsert fetched events for one account."""
    existing_by_uid: dict[str, CalendarEvent] = {}
    if fetched:
        rows = await session.execute(
            select(CalendarEvent).where(
                CalendarEvent.account_id == account_id,
                CalendarEvent.provider_uid.in_([fe.provider_uid for fe in fetched]),
            )
        )
        existing_by_uid = {ev.provider_uid: ev for ev in rows.scalars().all()}

    stored = updated = 0
    for fe in fetched:
        ev = existing_by_uid.get(fe.provider_uid)
        if ev is None:
            session.add(
                CalendarEvent(
                    account_id=account_id,
                    provider_uid=fe.provider_uid,
                    start_at=fe.start_at,
                    end_at=fe.end_at,
                    title=fe.title,
                    description=fe.description,
                    location=fe.location,
                    organizer=fe.organizer,
                    attendees=fe.attendees,
                    calendar_name=fe.calendar,
                )
            )
            stored += 1
            continue
        ev.start_at = fe.start_at
        ev.end_at = fe.end_at
        ev.title = fe.title
        ev.description = fe.description
        ev.location = fe.location
        ev.organizer = fe.organizer
        ev.attendees = fe.attendees
        ev.calendar_name = fe.calendar
        ev.cancelled_at = None  # present upstream again
        updated += 1
    await session.flush()
    for ev in (await session.execute(
        select(CalendarEvent).where(CalendarEvent.account_id == account_id)
    )).scalars():
        await search_index.index_event(session, ev)
    return {"stored": stored, "updated": updated}


async def sweep_cancelled(
    session: AsyncSession,
    account_id: uuid.UUID,
    fetched_uids: set[str],
    start: datetime,
    end: datetime,
) -> int:
    """Mark future events missing from a COMPLETE fetch as cancelled; retire their items.

    Only call when the fetch covered all calendars (complete=True) — a partial fetch
    would mass-cancel events that merely live on the calendar that errored."""
    rows = (
        await session.execute(
            select(CalendarEvent).where(
                CalendarEvent.account_id == account_id,
                CalendarEvent.start_at >= start,
                CalendarEvent.start_at <= end,
                CalendarEvent.cancelled_at.is_(None),
            )
        )
    ).scalars().all()
    stale = [ev for ev in rows if ev.provider_uid not in fetched_uids]
    if not stale:
        return 0
    now = datetime.now(UTC)
    for ev in stale:
        ev.cancelled_at = now
    await session.flush()
    return len(stale)


async def sync_account_calendar(
    session: AsyncSession,
    account: Account,
    password: str,
    now: datetime,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> dict:
    """Fetch this account's upcoming events and store them."""
    base = caldav.caldav_base_url(account.provider)
    if not base:
        log.info("No CalDAV base URL for provider %r — skipping calendar", account.provider)
        return {"skipped": True}

    start, end = now, now + timedelta(days=horizon_days)
    summary: dict = {}
    try:
        fetched, complete = await caldav.fetch_events(base, account.login, password, start, end)
    except Exception as exc:  # noqa: BLE001 — offline CalDAV must not block DB-side upkeep
        log.warning("Calendar fetch failed for %s: %s", account.name, exc)
        summary["fetch_error"] = str(exc)[:200]
    else:
        summary["fetched"] = len(fetched)
        summary.update(await store_events(session, account.id, fetched))
        if complete:
            summary["cancelled"] = await sweep_cancelled(
                session, account.id, {fe.provider_uid for fe in fetched}, start, end
            )
        await session.commit()  # следующий шаг ходит в сеть — не держать запись открытой
        summary["named"] = await name_past_events(session, account, base, password, now)

    return summary


async def name_past_events(
    session: AsyncSession, account: Account, base: str, password: str, now: datetime
) -> int:
    """Один раз на ящик: календарь у уже лежащих прошедших встреч. Только имя —
    ни новых строк, ни отмен: прошлое сверка не ведёт."""
    done_key = _NAMES_FILLED.format(account.id)
    if await settings_store.get_value(session, done_key):
        return 0
    try:
        fetched, complete = await caldav.fetch_events(
            base, account.login, password, now - timedelta(days=NAMES_BACKFILL_DAYS), now
        )
    except Exception as exc:  # noqa: BLE001 — не вышло сейчас, выйдет на следующей сверке
        log.warning("Calendar names backfill failed for %s: %s", account.name, exc)
        return 0
    names = {fe.provider_uid: fe.calendar for fe in fetched if fe.calendar}
    rows = (await session.execute(
        select(CalendarEvent).where(
            CalendarEvent.account_id == account.id,
            CalendarEvent.calendar_name.is_(None),
            CalendarEvent.provider_uid.in_(names),
        )
    )).scalars().all()
    for ev in rows:
        ev.calendar_name = names[ev.provider_uid]
    if complete:
        await settings_store.set_value(session, done_key, now.isoformat())
    return len(rows)


async def sync_all_accounts(now: datetime, horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict:
    """Poll every enabled mailbox with a CalDAV calendar and refresh prep items.

    Per-account isolation: one account's failure (bad creds, unreachable server) never
    aborts the others, and each runs in its own transaction.
    """
    from email_reader.accounts import resolve_caldav_password

    async with session_scope() as session:
        accounts = list((await session.execute(select(Account))).scalars().all())
    results: dict = {}
    for acc in accounts:
        if not acc.enabled or not caldav.caldav_base_url(acc.provider):
            continue
        try:
            password = resolve_caldav_password(acc)
        except Exception as exc:  # noqa: BLE001 — skip mailboxes we can't authenticate
            log.warning("Calendar sync: no password for %s: %s", acc.name, exc)
            continue
        try:
            async with session_scope() as session:
                fresh = await session.get(Account, acc.id)
                results[acc.name] = await sync_account_calendar(
                    session, fresh, password, now, horizon_days
                )
        except Exception as exc:  # noqa: BLE001 — one mailbox must not break the rest
            log.warning("Calendar sync failed for %s: %s", acc.name, exc)
    return results
