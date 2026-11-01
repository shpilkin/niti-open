"""Identity service: the set of addresses that are "me".

DB-backed and editable from the UI. Imported from config.yaml on first run.
Direction (inbound/outbound/self) at ingest time is computed against this set, so
changing it only affects newly synced mail (existing emails keep their stored direction).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.config import AppConfig
from email_reader.models import Identity
from email_reader.normalization import normalize_addr


async def list_identities(session: AsyncSession) -> list[Identity]:
    return list(
        (await session.execute(select(Identity).order_by(Identity.email))).scalars().all()
    )


async def get_identity_set(session: AsyncSession) -> set[str]:
    rows = await session.execute(select(Identity.email))
    return {e for (e,) in rows}


async def add_identity(session: AsyncSession, email: str) -> Identity | None:
    norm = normalize_addr(email)
    if not norm or "@" not in norm:
        return None
    exists = (
        await session.execute(select(Identity).where(Identity.email == norm))
    ).scalar_one_or_none()
    if exists is not None:
        return exists
    ident = Identity(email=norm)
    session.add(ident)
    await session.flush()
    return ident


async def remove_identity(session: AsyncSession, identity_id: uuid.UUID) -> None:
    ident = (
        await session.execute(select(Identity).where(Identity.id == identity_id))
    ).scalar_one_or_none()
    if ident is not None:
        await session.delete(ident)


async def import_config_if_empty(session: AsyncSession, cfg: AppConfig) -> int:
    count = (await session.execute(select(func.count()).select_from(Identity))).scalar_one()
    if count > 0:
        return 0
    created = 0
    for email in cfg.identities:
        if await add_identity(session, email) is not None:
            created += 1
    return created
