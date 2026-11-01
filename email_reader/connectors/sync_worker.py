"""Sync worker: poll enabled DB accounts, normalize, store (with dedup), enqueue.

Accounts live in the DB (UI-managed). On first run, config.yaml accounts are imported.
Each account folder is polled on an interval; per-account status (last_sync_at/last_error)
is recorded for the Settings UI.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.accounts import import_config_if_empty, list_accounts, resolve_password
from email_reader.config import get_app_config
from email_reader.connectors.imap import fetch_new
from email_reader.db import session_scope
from email_reader.identities import get_identity_set
from email_reader.identities import import_config_if_empty as import_identities_if_empty
from email_reader.ingest import store_email
from email_reader.models import Account, Email, EmailOccurrence, SyncState
from email_reader.normalization import parse_raw_email

log = logging.getLogger("email_reader.sync")

# Писем за одну транзакцию. 2 мс на письмо на этой машине, то есть замок базы держится
# доли секунды на пачку — а не минуты, как при первой синхронизации целиком.
STORE_BATCH = 200


async def _get_state(session: AsyncSession, account_id, folder: str) -> SyncState:
    st = (
        await session.execute(
            select(SyncState).where(
                SyncState.account_id == account_id, SyncState.folder == folder
            )
        )
    ).scalar_one_or_none()
    if st is None:
        st = SyncState(account_id=account_id, folder=folder, last_uid=0)
        session.add(st)
        await session.flush()
    return st


async def _unread_uids(
    session: AsyncSession, account_id, folder: str, window_days: int | None = None
) -> list[int]:
    """Locally-unread UIDs in (account, folder), bounded to the sync window so an inbox of
    never-read newsletters doesn't turn every poll into dozens of SEARCH round trips."""
    q = (
        select(EmailOccurrence.uid)
        .join(Email, Email.id == EmailOccurrence.email_id)
        .where(
            EmailOccurrence.account_id == account_id,
            EmailOccurrence.folder == folder,
            Email.read_at.is_(None),
        )
    )
    if window_days is not None:
        q = q.where(Email.sent_at >= datetime.now(UTC) - timedelta(days=window_days))
    rows = await session.execute(q)
    return sorted(int(u) for u in rows.scalars().all())


# SQLite binds at most 32766 parameters per statement; stay far below it.
_ABSORB_CHUNK = 500


async def absorb_server_seen(
    session: AsyncSession, account_id, folder: str, seen_uids: set[int],
    now: datetime | None = None,
) -> int:
    """Mark locally-unread emails read when the server already has them \\Seen — they
    were read in another client. Writes read_at directly, with NO MarkSeen event: the
    server is already right, echoing the flag back would only cost a round trip. read_at
    here is the sync time, not the moment of reading. Returns how many were marked."""
    if not seen_uids:
        return 0
    now = now or datetime.now(UTC)
    uids = sorted(seen_uids)
    marked: set = set()
    for i in range(0, len(uids), _ABSORB_CHUNK):
        rows = await session.execute(
            select(Email)
            .join(EmailOccurrence, EmailOccurrence.email_id == Email.id)
            .where(
                EmailOccurrence.account_id == account_id,
                EmailOccurrence.folder == folder,
                EmailOccurrence.uid.in_(uids[i:i + _ABSORB_CHUNK]),
                Email.read_at.is_(None),
            )
        )
        for e in rows.scalars().all():
            if e.id not in marked:
                e.read_at = now
                marked.add(e.id)
    return len(marked)


async def sync_folder(account: Account, password: str, folder: str, identities: set[str],
                      normalize_plus_tags: bool, sync_window_days: int | None = None) -> int:
    """Sync one (account, folder). Returns count of new emails stored."""
    async with session_scope() as session:
        st = await _get_state(session, account.id, folder)
        last_uid, stored_validity = st.last_uid, st.uidvalidity
        unread = await _unread_uids(session, account.id, folder, sync_window_days)

    result = await fetch_new(
        host=account.host, port=account.port, login=account.login,
        password=password, folder=folder, last_uid=last_uid,
        since_days=sync_window_days, unread_uids=unread,
    )

    # UIDVALIDITY changed -> server renumbered; restart this folder from 0.
    if (
        result.uidvalidity is not None
        and stored_validity is not None
        and result.uidvalidity != stored_validity
        and last_uid > 0
    ):
        log.warning("UIDVALIDITY reset on %s/%s; restarting cursor", account.name, folder)
        async with session_scope() as session:
            st = await _get_state(session, account.id, folder)
            st.last_uid = 0
            st.uidvalidity = result.uidvalidity
        result = await fetch_new(
            host=account.host, port=account.port, login=account.login,
            password=password, folder=folder, last_uid=0,
            since_days=sync_window_days,
        )

    # Read elsewhere? Runs even when no new mail arrived — read-state changes on its own.
    # After a UIDVALIDITY reset `result` is the refetch, whose seen_uids is empty (the
    # UIDs we asked about were stale), so nothing wrong can be absorbed.
    if result.seen_uids:
        async with session_scope() as session:
            absorbed = await absorb_server_seen(session, account.id, folder, result.seen_uids)
        if absorbed:
            log.info("Read elsewhere on %s/%s: %d marked read", account.name, folder, absorbed)

    if not result.messages:
        if result.uidvalidity is not None and stored_validity is None:
            async with session_scope() as session:
                st = await _get_state(session, account.id, folder)
                st.uidvalidity = result.uidvalidity
        return 0

    count = 0
    # By the batch, not in one go: the first sync of a mailbox brings thousands of letters,
    # and while they are being written nobody else can write at all. Sorted by UID, so the
    # cursor a batch commits never skips a letter still to come.
    messages = sorted(result.messages, key=lambda m: m.uid)
    for start in range(0, len(messages), STORE_BATCH):
        batch = messages[start:start + STORE_BATCH]
        async with session_scope() as session:
            for msg in batch:
                try:
                    parsed = parse_raw_email(msg.raw)
                except Exception:  # noqa: BLE001
                    log.exception("Parse failed %s/%s uid=%s", account.name, folder, msg.uid)
                    continue
                _, is_new = await store_email(
                    session, account_id=account.id, folder=folder, uid=msg.uid,
                    parsed=parsed, identities=identities,
                    normalize_plus_tags=normalize_plus_tags,
                )
                if is_new:
                    count += 1
            st = await _get_state(session, account.id, folder)
            st.last_uid = max([st.last_uid] + [m.uid for m in batch])
            if result.uidvalidity is not None:
                st.uidvalidity = result.uidvalidity
    log.info("Synced %s/%s: %d new", account.name, folder, count)
    return count


def _remap_folders(
    configured: list[str], available: list[str], suggested_sent: str | None
) -> list[str]:
    """Keep configured folders that still exist on the server; a vanished non-INBOX folder
    (Gmail localizes/renames "[Gmail]/Sent Mail" per account language) is remapped to the
    server's SPECIAL-USE sent folder. An unresolvable folder is KEPT as-is (not dropped) so
    its error stays visible rather than silently ceasing to sync. Pure/testable."""
    avail = set(available)
    out: list[str] = []
    for f in configured:
        if f.upper() == "INBOX" or f in avail:
            target = f
        elif suggested_sent and suggested_sent in avail:
            target = suggested_sent            # relocate the drifted sent folder
        else:
            target = f                         # unresolvable — keep so the error surfaces
        if target not in out:
            out.append(target)
    return out


async def _heal_folders(account: Account, password: str) -> str | None:
    """A configured folder wasn't found on the server. Re-LIST, remap the sent folder via
    SPECIAL-USE, persist the corrected list, and return the resolved sent folder name to
    retry (or None if it couldn't be resolved — e.g. hidden by Gmail's "Show in IMAP")."""
    from email_reader import providers

    res = await providers.test_connection(
        account.host, account.port, account.login, password
    )
    if not res.ok:
        return None
    before = list(account.folders)
    fixed = _remap_folders(before, res.folders, res.suggested_sent)
    if fixed == before:
        return None
    async with session_scope() as session:
        acc = (
            await session.execute(select(Account).where(Account.id == account.id))
        ).scalar_one_or_none()
        if acc is not None:
            acc.folders = fixed
    account.folders = fixed  # keep the in-memory copy in sync for this cycle
    log.info("Remapped folders for %s: %s -> %s", account.name, before, fixed)
    return res.suggested_sent if res.suggested_sent in fixed else None


async def _set_account_status(account_id, error: str | None) -> None:
    async with session_scope() as session:
        acc = (
            await session.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()
        if acc is not None:
            acc.last_sync_at = datetime.now(UTC)
            acc.last_error = error


async def sync_once(cfg, account_id=None) -> int:
    """Sync enabled mailboxes. When account_id is given, sync only that one mailbox."""
    async with session_scope() as session:
        accounts = [a for a in await list_accounts(session) if a.enabled]
        if account_id is not None:
            accounts = [a for a in accounts if a.id == account_id]
        identities = await get_identity_set(session)
    total = 0
    for account in accounts:
        try:
            password = resolve_password(account)
        except Exception as exc:  # noqa: BLE001
            await _set_account_status(account.id, f"нет пароля: {exc}")
            continue
        error: str | None = None
        healed = False
        for folder in list(account.folders):
            try:
                total += await sync_folder(
                    account, password, folder, identities, cfg.normalize_plus_tags,
                    sync_window_days=cfg.sync_window_days,
                )
            except FileNotFoundError as exc:
                # Folder name drifted from the server (Gmail localizes [Gmail]/Sent Mail).
                # Re-resolve via SPECIAL-USE once per cycle and retry the corrected folder.
                resolved = None if healed else await _heal_folders(account, password)
                healed = True
                if resolved and resolved != folder:
                    try:
                        total += await sync_folder(
                            account, password, resolved, identities,
                            cfg.normalize_plus_tags, sync_window_days=cfg.sync_window_days,
                        )
                        continue
                    except Exception as exc2:  # noqa: BLE001
                        log.exception("Sync failed for %s/%s (healed)", account.name, resolved)
                        error = f"{resolved}: {exc2}"
                        continue
                log.warning("Folder not found, unresolved: %s/%s", account.name, folder)
                error = f"{folder}: {exc}"
            except Exception as exc:  # noqa: BLE001
                log.exception("Sync failed for %s/%s", account.name, folder)
                error = f"{folder}: {exc}"
        await _set_account_status(account.id, error)
    return total


async def run_sync_loop(stop: asyncio.Event | None = None) -> None:
    cfg = get_app_config()
    async with session_scope() as session:
        imported = await import_config_if_empty(session, cfg)
        ident_imported = await import_identities_if_empty(session, cfg)
    if imported:
        log.info("Imported %d account(s) from config.yaml", imported)
    if ident_imported:
        log.info("Imported %d identity(ies) from config.yaml", ident_imported)
    log.info("Sync loop started (interval=%ss)", cfg.poll_interval_seconds)
    while stop is None or not stop.is_set():
        try:
            await sync_once(cfg)
        except Exception:  # noqa: BLE001
            log.exception("sync_once failed")
        await asyncio.sleep(cfg.poll_interval_seconds)
