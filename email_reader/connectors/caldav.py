"""CalDAV connector: read-only fetch of calendar events.

Mirrors the IMAP connector: a blocking implementation run off the event loop with
``asyncio.to_thread``. v1 reuses the mailbox app-password (same credential as IMAP) and
derives the CalDAV base URL from the provider. Recurring events are expanded server-side
over the requested date range, so no client-side RRULE handling is needed.

``parse_ical`` is a pure function (no network) and carries the parsing logic that tests
exercise; ``fetch_events`` is the live path that needs a real server.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from dataclasses import dataclass, field

log = logging.getLogger("email_reader.caldav")

# Provider -> CalDAV base URL (principal discovery starts here). Reuses the IMAP app-password.
CALDAV_BASE = {
    "yandex": "https://caldav.yandex.ru",
    "gmail": "https://apidata.googleusercontent.com/caldav/v2/",
}

_TIMEOUT = 30  # seconds; a hung CalDAV call must not wedge the worker thread.


def caldav_base_url(provider: str) -> str | None:
    return CALDAV_BASE.get((provider or "").lower())


@dataclass
class FetchedEvent:
    provider_uid: str
    start_at: _dt.datetime
    end_at: _dt.datetime | None
    title: str | None
    description: str | None
    location: str | None
    organizer: str | None
    attendees: list = field(default_factory=list)  # [{"email","name"}]
    calendar: str | None = None  # имя календаря ящика, из которого пришло событие


def _to_utc(value) -> _dt.datetime | None:
    """Normalize an iCalendar date/datetime to a tz-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=_dt.UTC)
        return value.astimezone(_dt.UTC)
    if isinstance(value, _dt.date):  # all-day event -> midnight UTC
        return _dt.datetime(value.year, value.month, value.day, tzinfo=_dt.UTC)
    return None


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _addr(cal_addr) -> str:
    """'mailto:john@x.ru' (any case) -> 'john@x.ru'."""
    s = str(cal_addr).strip()
    return s[7:] if s[:7].lower() == "mailto:" else s


def _attendees(comp) -> list[dict]:
    raw = comp.get("ATTENDEE")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[dict] = []
    for a in items:
        email = _addr(a).lower()
        if not email:
            continue
        name = None
        params = getattr(a, "params", None)
        if params is not None:
            name = _clean(params.get("CN"))
        out.append({"email": email, "name": name})
    return out


def _event_from_vevent(comp) -> FetchedEvent | None:
    dtstart = comp.get("DTSTART")
    uid = _clean(comp.get("UID"))
    if dtstart is None or not uid:
        return None
    start = _to_utc(dtstart.dt)
    if start is None:
        return None
    dtend = comp.get("DTEND")
    end = _to_utc(dtend.dt) if dtend is not None else None
    org = comp.get("ORGANIZER")
    # Stable dedup key: a plain event keeps its bare UID, so rescheduling updates the
    # same row instead of inserting a duplicate. Expanded recurrence instances share a
    # UID and are keyed by their RECURRENCE-ID (falling back to the instance start).
    rid = comp.get("RECURRENCE-ID")
    if rid is not None:
        rid_dt = _to_utc(rid.dt)
        provider_uid = f"{uid}#{rid_dt.isoformat() if rid_dt else start.isoformat()}"
    else:
        provider_uid = uid
    return FetchedEvent(
        provider_uid=provider_uid,
        start_at=start,
        end_at=end,
        title=_clean(comp.get("SUMMARY")),
        description=_clean(comp.get("DESCRIPTION")),
        location=_clean(comp.get("LOCATION")),
        organizer=_addr(org).lower() if org is not None else None,
        attendees=_attendees(comp),
    )


def parse_ical(data) -> list[FetchedEvent]:
    """Parse iCalendar text/bytes into FetchedEvent[]. Pure (no network)."""
    from icalendar import Calendar

    events: list[FetchedEvent] = []
    # A CalDAV multiget/report may return one VCALENDAR per object.
    for cal in Calendar.from_ical(data, multiple=True):
        for comp in cal.walk("VEVENT"):
            ev = _event_from_vevent(comp)
            if ev is not None:
                events.append(ev)
    return events


def _calendar_name(cal) -> str | None:  # noqa: ANN001
    try:
        return cal.name or None
    except Exception:  # noqa: BLE001 — имя берётся отдельным запросом PROPFIND; нет — так нет
        return None


def _fetch_blocking(
    base_url: str, username: str, password: str, start: _dt.datetime, end: _dt.datetime
) -> tuple[list[FetchedEvent], bool]:
    import caldav

    out: list[FetchedEvent] = []
    seen: set[str] = set()
    complete = True  # False when any calendar failed — cancellation sweep must be skipped
    with caldav.DAVClient(
        url=base_url, username=username, password=password, timeout=_TIMEOUT
    ) as client:
        principal = client.principal()
        for cal in principal.calendars():
            try:
                results = cal.search(start=start, end=end, event=True, expand=True)
            except Exception as exc:  # noqa: BLE001 — one bad calendar must not kill the rest
                log.warning("CalDAV search failed on %s: %s", cal, exc)
                complete = False
                continue
            name = _calendar_name(cal)
            for obj in results:
                for ev in parse_ical(obj.data):
                    ev.calendar = name
                    if ev.provider_uid not in seen:
                        seen.add(ev.provider_uid)
                        out.append(ev)
    return out, complete


def _test_blocking(base_url: str, username: str, password: str) -> str | None:
    import caldav

    try:
        with caldav.DAVClient(
            url=base_url, username=username, password=password, timeout=_TIMEOUT
        ) as client:
            cals = client.principal().calendars()
        return None if cals is not None else "Календари не найдены"
    except Exception as exc:  # noqa: BLE001 — surface a readable reason to the UI
        return str(exc)


async def test_connection(base_url: str, username: str, password: str) -> str | None:
    """Verify CalDAV auth + calendar discovery. None on success, else an error string."""
    return await asyncio.to_thread(_test_blocking, base_url, username, password)


async def fetch_events(
    base_url: str, username: str, password: str, start: _dt.datetime, end: _dt.datetime
) -> tuple[list[FetchedEvent], bool]:
    """Fetch events in [start, end] (recurrences expanded).

    Returns (events, complete): complete=False means at least one calendar errored, so
    the result may be partial and must not be used to infer cancellations."""
    return await asyncio.to_thread(_fetch_blocking, base_url, username, password, start, end)
