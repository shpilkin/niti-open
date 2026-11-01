"""IMAP connector via stdlib imaplib, run off the event loop with asyncio.to_thread.

Polling-based incremental sync keyed on (UIDVALIDITY, UID). Mailbox is opened
read-only so messages are never marked \\Seen — the system is read-only toward mail.
Each poll also asks which of the caller's locally-unread UIDs the server already has
\\Seen (read in another client), on the same connection — see `_search_seen`.
IDLE (near-real-time) is deferred; polling is the reliable mechanism for MVP.
"""

from __future__ import annotations

import asyncio
import imaplib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY\s+(\d+)")

# Locale-independent month abbreviations for the IMAP SINCE date format
# (dd-Mmm-yyyy). strftime("%b") would depend on the process locale.
_IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _imap_date(d: date) -> str:
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"


def _mailbox(folder: str) -> str:
    """A mailbox name as an IMAP quoted string. imaplib doesn't quote names itself, so a
    folder containing a space — e.g. Gmail's "[Gmail]/Sent Mail" — makes STATUS/SELECT
    fail with BAD "Could not parse command". Quoting is valid for plain names too, and
    imaplib leaves an already-quoted arg untouched, so this is safe everywhere."""
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class FetchedMessage:
    uid: int
    raw: bytes


@dataclass
class FetchResult:
    uidvalidity: int | None
    messages: list[FetchedMessage]
    seen_uids: set[int] = field(default_factory=set)  # of the caller's unread_uids


def _parse_uidvalidity(data) -> int | None:
    for part in data or []:
        if isinstance(part, (bytes, bytearray)):
            m = _UIDVALIDITY_RE.search(part)
            if m:
                return int(m.group(1))
    return None


# Cap every blocking IMAP socket op: without this a stalled/unresponsive server
# wedges the worker thread forever (imaplib defaults to no timeout).
_IMAP_TIMEOUT = 30

# UIDs per `UID SEARCH UID <set> SEEN` command: keeps each command line short enough for
# every server while a mailbox with thousands of unread messages still needs few round trips.
_SEEN_CHUNK = 200


def _search_seen(conn, uids: list[int]) -> set[int]:
    """Which of `uids` the server has flagged \\Seen. Asks only about the given UIDs (the
    caller's locally-unread ones) — never a date window or the whole folder, so the answer
    is exact and cheap regardless of mailbox size."""
    seen: set[int] = set()
    for i in range(0, len(uids), _SEEN_CHUNK):
        chunk = ",".join(str(u) for u in uids[i:i + _SEEN_CHUNK])
        typ, data = conn.uid("search", None, "UID", chunk, "SEEN")
        if typ != "OK" or not data or not data[0]:
            continue
        seen.update(int(x) for x in data[0].split())
    return seen


def _fetch_blocking(
    host: str, port: int, login: str, password: str, folder: str, last_uid: int,
    since_days: int | None = None, unread_uids: list[int] | None = None,
) -> FetchResult:
    conn = imaplib.IMAP4_SSL(host, port, timeout=_IMAP_TIMEOUT)
    try:
        conn.login(login, password)
        # Get UIDVALIDITY before selecting so a reset is detected by the caller.
        typ, data = conn.status(_mailbox(folder), "(UIDVALIDITY)")
        uidvalidity = _parse_uidvalidity(data) if typ == "OK" else None

        # Verify the folder exists/selects; otherwise imaplib stays in AUTH state
        # and the next SEARCH fails with a cryptic "illegal in state AUTH" error.
        typ, _ = conn.select(_mailbox(folder), readonly=True)
        if typ != "OK":
            raise FileNotFoundError(f"folder {folder!r} not found on server")
        seen = _search_seen(conn, unread_uids or [])
        start = last_uid + 1
        # Bound the window to the last `since_days` (server arrival date) so we
        # never try to pull an entire multi-year mailbox — large fetches get the
        # connection dropped by the server and aren't wanted locally anyway.
        criteria: list[str] = ["UID", f"{start}:*"]
        if since_days is not None:
            cutoff = date.today() - timedelta(days=since_days)
            criteria += ["SINCE", _imap_date(cutoff)]
        typ, data = conn.uid("search", None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return FetchResult(uidvalidity, [], seen)

        uids = [int(x) for x in data[0].split()]
        # `UID n:*` returns the last message even when none are >= n — filter those.
        uids = sorted(u for u in uids if u > last_uid)

        messages: list[FetchedMessage] = []
        for uid in uids:
            typ, fetched = conn.uid("fetch", str(uid), "(RFC822)")
            if typ != "OK" or not fetched:
                continue
            for part in fetched:
                if isinstance(part, tuple) and len(part) == 2 and part[1]:
                    messages.append(FetchedMessage(uid=uid, raw=part[1]))
                    break
        return FetchResult(uidvalidity, messages, seen)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


async def fetch_new(
    *, host: str, port: int, login: str, password: str, folder: str, last_uid: int,
    since_days: int | None = None, unread_uids: list[int] | None = None,
) -> FetchResult:
    """Fetch messages with UID > last_uid from `folder`, optionally limited to the
    last `since_days`; also report which of `unread_uids` the server has \\Seen.
    Runs in a worker thread."""
    return await asyncio.to_thread(
        _fetch_blocking, host, port, login, password, folder, last_uid, since_days,
        unread_uids,
    )


def _fetch_one_blocking(
    host: str, port: int, login: str, password: str, folder: str, uid: int,
    timeout: int = _IMAP_TIMEOUT,
) -> bytes | None:
    conn = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    try:
        conn.login(login, password)
        typ, _ = conn.select(_mailbox(folder), readonly=True)
        if typ != "OK":
            return None
        typ, fetched = conn.uid("fetch", str(uid), "(RFC822)")
        if typ != "OK" or not fetched:
            return None
        for part in fetched:
            if isinstance(part, tuple) and len(part) == 2 and part[1]:
                return part[1]
        return None
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


async def fetch_raw(
    *, host: str, port: int, login: str, password: str, folder: str, uid: int,
    imap_timeout: int = _IMAP_TIMEOUT,
) -> bytes | None:
    """Fetch the full RFC822 bytes of a single message by UID (for attachments).

    `imap_timeout` overrides the IMAP socket timeout — on-demand fetches use a shorter
    one so an unreachable server fails fast instead of spinning the UI.
    """
    return await asyncio.to_thread(
        _fetch_one_blocking, host, port, login, password, folder, uid, imap_timeout
    )


# Prefix marker for the "UIDVALIDITY changed, skipped" outcome — a *permanent* condition
# the caller should not retry (vs a transient connection error, which it should).
UIDVALIDITY_SKIP = "uidvalidity-skip"


def _set_seen_blocking(
    host: str, port: int, login: str, password: str, folder: str, uid: int, seen: bool,
    expected_uidvalidity: int | None = None, timeout: int = _IMAP_TIMEOUT,
) -> str | None:
    """Set (seen=True) or clear (seen=False) the \\Seen flag on one message by UID.

    Returns None on success or a short error string. If expected_uidvalidity is given and
    the folder's current UIDVALIDITY differs, the UID no longer identifies the same message
    (the folder was renumbered), so we DON'T touch anything and return a UIDVALIDITY_SKIP
    marker — flagging the wrong mail on the user's real server would be far worse than not
    syncing this one read receipt.
    """
    conn = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    try:
        conn.login(login, password)
        if expected_uidvalidity is not None:
            typ, data = conn.status(_mailbox(folder), "(UIDVALIDITY)")
            current = _parse_uidvalidity(data) if typ == "OK" else None
            if current is not None and current != expected_uidvalidity:
                return f"{UIDVALIDITY_SKIP}: {current} != {expected_uidvalidity}"
        # Read-WRITE select (unlike sync, which is read-only) so STORE can set flags.
        typ, _ = conn.select(_mailbox(folder), readonly=False)
        if typ != "OK":
            raise FileNotFoundError(f"folder {folder!r} not selectable")
        op = "+FLAGS" if seen else "-FLAGS"
        typ, _ = conn.uid("STORE", str(uid), op, "(\\Seen)")
        if typ != "OK":
            return f"STORE {op} \\Seen failed on uid {uid}: {typ}"
        return None
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


async def set_seen(
    *, host: str, port: int, login: str, password: str, folder: str, uid: int, seen: bool,
    expected_uidvalidity: int | None = None, imap_timeout: int = 10,
) -> str | None:
    """Set/clear \\Seen on a message by UID, off the event loop. Short default timeout so a
    dead server fails fast instead of stalling the worker (read-state is best-effort)."""
    return await asyncio.to_thread(
        _set_seen_blocking, host, port, login, password, folder, uid, seen,
        expected_uidvalidity, imap_timeout,
    )
