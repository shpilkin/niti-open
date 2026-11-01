"""Account service: password resolution, config bootstrap, CRUD used by UI + sync.

DB is the source of truth for accounts. config.yaml is imported once on first run
(when the DB has no accounts) so existing env-based setups keep working; after that,
accounts are managed via the UI and passwords are stored encrypted (password_enc).
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import spaces as spaces_svc
from email_reader.config import AppConfig
from email_reader.crypto import decrypt, encrypt
from email_reader.identities import add_identity
from email_reader.models import Account, Email, EmailOccurrence, SyncState, Thread

# Sentinel for "argument not supplied" so update_account can set a field to None
# explicitly (e.g. clear a per-mailbox model) vs. leave it unchanged.


def guess_provider(host: str) -> str:
    h = (host or "").lower()
    if "gmail" in h or "google" in h:
        return "gmail"
    if "yandex" in h or "ya.ru" in h:
        return "yandex"
    return "imap"


def resolve_password(account: Account) -> str:
    """Decrypt the stored password, or fall back to the env var (config-imported accounts)."""
    if account.password_enc:
        return decrypt(account.password_enc)
    if account.password_ref:
        pw = os.environ.get(account.password_ref, "")
        if not pw:
            raise RuntimeError(
                f"App-password env var {account.password_ref!r} for {account.name!r} is empty"
            )
        return pw
    raise RuntimeError(f"Account {account.name!r} has no stored password")


def resolve_caldav_password(account: Account) -> str:
    """Password for CalDAV: the dedicated calendar app-password if set, else the mailbox
    password. Yandex needs a separate one (per-service app-password scoping)."""
    if account.caldav_password_enc:
        return decrypt(account.caldav_password_enc)
    return resolve_password(account)


async def count_accounts(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Account))).scalar_one()


async def list_accounts(
    session: AsyncSession, space_id: uuid.UUID | None = None
) -> list[Account]:
    """All mailboxes, or only those of one space."""
    q = select(Account).order_by(Account.created_at)
    if space_id is not None:
        q = q.where(Account.space_id == space_id)
    return list((await session.execute(q)).scalars().all())


async def get_account(session: AsyncSession, account_id: uuid.UUID) -> Account | None:
    return (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()


async def import_config_if_empty(session: AsyncSession, cfg: AppConfig) -> int:
    """First-run bootstrap: copy config.yaml accounts into the DB if none exist yet."""
    if await count_accounts(session) > 0:
        return 0
    created = 0
    space = await spaces_svc.ensure_default(session)
    for acc in cfg.accounts:
        session.add(
            Account(
                name=acc.name,
                space_id=space.id,
                provider=guess_provider(acc.host),
                host=acc.host,
                port=acc.port,
                login=acc.login,
                password_ref=acc.password_ref,  # env-based; not encrypted
                folders=acc.folders,
            )
        )
        created += 1
    return created


async def create_account(
    session: AsyncSession,
    *,
    name: str,
    provider: str,
    host: str,
    port: int,
    login: str,
    password: str,
    folders: list[str],
    smtp_host: str | None = None,
    smtp_port: int = 465,
    caldav_password: str | None = None,
    space_id: uuid.UUID | None = None,  # None -> the default space
) -> Account:
    if space_id is None:
        space_id = (await spaces_svc.ensure_default(session)).id
    acc = Account(
        name=name,
        space_id=space_id,
        provider=provider,
        host=host,
        port=port,
        smtp_host=smtp_host or None,
        smtp_port=smtp_port,
        login=login,
        password_enc=encrypt(password),
        caldav_password_enc=encrypt(caldav_password) if caldav_password else None,
        folders=folders,
        enabled=True,
    )
    session.add(acc)
    await session.flush()
    # The account's own address is, by definition, "me" — register it as an identity
    # so the user's sent mail is classified outbound (and tracked as commitments).
    await add_identity(session, login)
    return acc


async def update_account(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    name: str | None = None,
    host: str | None = None,
    port: int | None = None,
    login: str | None = None,
    password: str | None = None,  # only re-encrypt when a new one is provided
    folders: list[str] | None = None,
    enabled: bool | None = None,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    caldav_password: str | None = None,  # blank/None = keep existing
    space_id: uuid.UUID | None = None,  # move the mailbox to another space
) -> Account | None:
    acc = await get_account(session, account_id)
    if acc is None:
        return None
    if name is not None:
        acc.name = name
    if host is not None:
        acc.host = host
    if port is not None:
        acc.port = port
    if smtp_host is not None:
        acc.smtp_host = smtp_host or None
    if smtp_port is not None:
        acc.smtp_port = smtp_port
    if login is not None:
        acc.login = login
        await add_identity(session, login)  # keep identities in sync with the login
    if password:
        acc.password_enc = encrypt(password)
        acc.password_ref = None
    if caldav_password:
        acc.caldav_password_enc = encrypt(caldav_password)
    if folders is not None:
        acc.folders = folders
    if enabled is not None:
        acc.enabled = enabled
    if space_id is not None:
        acc.space_id = space_id
    return acc


async def delete_account(session: AsyncSession, account_id: uuid.UUID) -> None:
    acc = await get_account(session, account_id)
    if acc is None:
        return
    # Emails/threads are per-account; detach them so the FK delete succeeds (the data is
    # kept but unscoped rather than cascade-deleted). Their items stay in the space.
    await session.execute(
        update(Email).where(Email.account_id == account_id).values(account_id=None)
    )
    await session.execute(
        update(Thread).where(Thread.account_id == account_id).values(account_id=None)
    )
    await session.execute(
        delete(EmailOccurrence).where(EmailOccurrence.account_id == account_id)
    )
    await session.execute(delete(SyncState).where(SyncState.account_id == account_id))
    await session.delete(acc)
