"""Propagate local read-state back to the IMAP server (set/clear \\Seen).

The app was historically read-only toward mail (imap.py opens folders read-only). This is
the ONE place that writes back. It runs from the worker as a `MarkSeen` event so a flaky
server retries with backoff instead of blocking the UI, and it is:

- convergent: it reads the email's CURRENT read_at (not a value frozen into the event), so
  repeated or reordered MarkSeen events for the same email always leave the server matching
  the final local state;
- safe: every write is guarded by UIDVALIDITY (see imap._set_seen_blocking) so a folder
  renumber can never flag the wrong message;
- mailbox-isolated: it only touches the occurrences (account/folder/uid) that actually hold
  the message.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.accounts import resolve_password
from email_reader.connectors import imap
from email_reader.models import Account, Email, EmailOccurrence, SyncState

log = logging.getLogger("email_reader.read_sync")


async def process_mark_seen(session: AsyncSession, payload: dict) -> None:
    """Sync one email's read state to the server for each of its occurrences."""
    raw_id = payload.get("email_id")
    if not raw_id:
        return
    email_id = uuid.UUID(raw_id)
    email = (
        await session.execute(select(Email).where(Email.id == email_id))
    ).scalar_one_or_none()
    if email is None:
        return
    seen = email.read_at is not None

    occurrences = (
        await session.execute(
            select(EmailOccurrence).where(EmailOccurrence.email_id == email_id)
        )
    ).scalars().all()
    if not occurrences:
        return

    accounts: dict[uuid.UUID, Account | None] = {}
    uidvalidity: dict[tuple[uuid.UUID, str], int | None] = {}
    retryable: list[str] = []  # transient failures -> raise so the event retries

    for occ in occurrences:
        if occ.account_id not in accounts:
            accounts[occ.account_id] = (
                await session.execute(select(Account).where(Account.id == occ.account_id))
            ).scalar_one_or_none()
        account = accounts[occ.account_id]
        if account is None or not account.enabled:
            continue
        try:
            password = resolve_password(account)
        except Exception as exc:  # noqa: BLE001 — no key -> can't sync this mailbox, skip
            log.warning("MarkSeen: no password for account %s: %s", occ.account_id, exc)
            continue

        key = (occ.account_id, occ.folder)
        if key not in uidvalidity:
            ss = (
                await session.execute(
                    select(SyncState).where(
                        SyncState.account_id == occ.account_id,
                        SyncState.folder == occ.folder,
                    )
                )
            ).scalar_one_or_none()
            uidvalidity[key] = ss.uidvalidity if ss else None

        err = await imap.set_seen(
            host=account.host, port=account.port, login=account.login, password=password,
            folder=occ.folder, uid=occ.uid, seen=seen,
            expected_uidvalidity=uidvalidity[key],
        )
        if err is None:
            continue
        if err.startswith(imap.UIDVALIDITY_SKIP):
            # Permanent: the UID is stale. Don't retry — just log and move on.
            log.info("MarkSeen: %s/%s#%s skipped (%s)", account.name, occ.folder, occ.uid, err)
        else:
            retryable.append(f"{account.name}/{occ.folder}#{occ.uid}: {err}")

    if retryable:
        # A transient server problem — raise so the worker reschedules with backoff.
        raise RuntimeError("MarkSeen incomplete: " + "; ".join(retryable))
