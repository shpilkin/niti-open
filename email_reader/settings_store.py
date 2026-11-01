"""Generic key-value store for UI-editable app settings (foundation for future settings).

Secrets are Fernet-encrypted at rest (is_secret=True). Currently unused by shipped
features (model connections live in their own table) but kept as the extension point for
simple scalar settings.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.crypto import decrypt, encrypt
from email_reader.models import AppSetting


async def get_value(session: AsyncSession, key: str) -> str | None:
    row = (
        await session.execute(select(AppSetting).where(AppSetting.key == key))
    ).scalar_one_or_none()
    if row is None or row.value is None:
        return None
    if row.is_secret:
        try:
            return decrypt(row.value)
        except Exception:  # noqa: BLE001 — bad/rotated key -> treat as unset
            return None
    return row.value


async def set_value(session: AsyncSession, key: str, value: str, secret: bool = False) -> None:
    stored = encrypt(value) if secret else value
    row = (
        await session.execute(select(AppSetting).where(AppSetting.key == key))
    ).scalar_one_or_none()
    if row is None:
        session.add(AppSetting(key=key, value=stored, is_secret=secret))
    else:
        row.value = stored
        row.is_secret = secret


async def delete_value(session: AsyncSession, key: str) -> None:
    row = (
        await session.execute(select(AppSetting).where(AppSetting.key == key))
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
