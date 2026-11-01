"""Service for LLM model connections (CRUD + default selection + resolution).

The default connection drives extraction. API keys are Fernet-encrypted. For a native
Anthropic connection with no stored key, the env ANTHROPIC_API_KEY is used. When no
connection exists at all, extraction falls back to env Anthropic settings (back-compat).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.ai_providers import AI_PROVIDERS
from email_reader.config import get_settings
from email_reader.crypto import decrypt, encrypt
from email_reader.models import Account, ModelConnection, Space, Thread


@dataclass
class ResolvedModel:
    kind: str
    base_url: str | None
    api_key: str | None
    model: str
    no_thinking: bool = False


async def list_connections(session: AsyncSession) -> list[ModelConnection]:
    return list(
        (
            await session.execute(select(ModelConnection).order_by(ModelConnection.created_at))
        ).scalars().all()
    )


async def get_connection(session: AsyncSession, cid: uuid.UUID) -> ModelConnection | None:
    return (
        await session.execute(select(ModelConnection).where(ModelConnection.id == cid))
    ).scalar_one_or_none()


async def _count(session: AsyncSession) -> int:
    return len((await session.execute(select(ModelConnection.id))).all())


async def create_connection(
    session: AsyncSession,
    *,
    name: str,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    no_thinking: bool = False,
) -> ModelConnection:
    preset = AI_PROVIDERS.get(provider, AI_PROVIDERS["openai_custom"])
    kind = preset["kind"]
    first = await _count(session) == 0
    conn = ModelConnection(
        name=name,
        provider=provider,
        kind=kind,
        base_url=(base_url.strip() or None) if kind == "openai" else None,
        api_key_enc=encrypt(api_key) if api_key.strip() else None,
        model=model.strip(),
        is_default=first,
        enabled=True,
        no_thinking=no_thinking,
    )
    session.add(conn)
    await session.flush()
    return conn


async def update_connection(
    session: AsyncSession,
    cid: uuid.UUID,
    *,
    name: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,  # only re-encrypt when provided
    enabled: bool | None = None,
    no_thinking: bool | None = None,
) -> ModelConnection | None:
    conn = await get_connection(session, cid)
    if conn is None:
        return None
    if name is not None:
        conn.name = name
    if base_url is not None and conn.kind == "openai":
        conn.base_url = base_url.strip() or None
    if model is not None:
        conn.model = model.strip()
    if api_key:
        conn.api_key_enc = encrypt(api_key)
    if enabled is not None:
        conn.enabled = enabled
    if no_thinking is not None:
        conn.no_thinking = no_thinking
    return conn


async def set_default(session: AsyncSession, cid: uuid.UUID) -> None:
    await session.execute(update(ModelConnection).values(is_default=False))
    await session.execute(
        update(ModelConnection).where(ModelConnection.id == cid).values(is_default=True)
    )


async def delete_connection(session: AsyncSession, cid: uuid.UUID) -> None:
    conn = await get_connection(session, cid)
    if conn is None:
        return
    was_default = conn.is_default
    await session.delete(conn)
    await session.flush()
    if was_default:
        other = (
            await session.execute(select(ModelConnection).limit(1))
        ).scalar_one_or_none()
        if other is not None:
            other.is_default = True


def _resolve_key(conn: ModelConnection) -> str | None:
    if conn.api_key_enc:
        try:
            return decrypt(conn.api_key_enc)
        except Exception:  # noqa: BLE001
            return None
    if conn.kind == "anthropic":
        return get_settings().anthropic_api_key or None
    return None


def resolve_connection(conn: ModelConnection | None) -> ResolvedModel | None:
    """One connection as a usable model, or None if it cannot serve requests at all
    (missing, switched off, or its key does not decrypt)."""
    if conn is None or not conn.enabled:
        return None
    key = _resolve_key(conn)
    if not key:
        return None
    return ResolvedModel(
        kind=conn.kind, base_url=conn.base_url, api_key=key, model=conn.model,
        no_thinking=conn.no_thinking,
    )


async def is_model_configured(session: AsyncSession) -> bool:
    """True when extraction has a usable model (a key resolves)."""
    rm = await resolve_active(session)
    return bool(rm.api_key)


async def resolve_active(session: AsyncSession) -> ResolvedModel:
    """The connection used for extraction. Falls back to env Anthropic if none configured."""
    conn = (
        await session.execute(
            select(ModelConnection)
            .where(ModelConnection.is_default.is_(True), ModelConnection.enabled.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    if conn is None:
        conn = (
            await session.execute(
                select(ModelConnection).where(ModelConnection.enabled.is_(True)).limit(1)
            )
        ).scalar_one_or_none()
    if conn is not None:
        return ResolvedModel(
            kind=conn.kind,
            base_url=conn.base_url,
            api_key=_resolve_key(conn),
            model=conn.model,
            no_thinking=conn.no_thinking,
        )
    settings = get_settings()
    return ResolvedModel(
        kind="anthropic",
        base_url=None,
        api_key=settings.anthropic_api_key or None,
        model=settings.extraction_model,
    )


async def resolve_for_space(
    session: AsyncSession, space_id: uuid.UUID | None
) -> ResolvedModel:
    """The model for a space: its own connection if set & usable, else the global
    default. This is the isolation point — a space's model only ever sees that space."""
    if space_id is not None:
        space = (
            await session.execute(select(Space).where(Space.id == space_id))
        ).scalar_one_or_none()
        if space is not None and space.model_connection_id is not None:
            rm = resolve_connection(await get_connection(session, space.model_connection_id))
            if rm is not None:
                return rm
    return await resolve_active(session)


async def resolve_for_account(
    session: AsyncSession, account_id: uuid.UUID | None
) -> ResolvedModel:
    """The model for a mailbox = the model of the space the mailbox feeds."""
    space_id = None
    if account_id is not None:
        space_id = (
            await session.execute(select(Account.space_id).where(Account.id == account_id))
        ).scalar_one_or_none()
    return await resolve_for_space(session, space_id)


async def resolve_for_thread(session: AsyncSession, thread_id: uuid.UUID) -> ResolvedModel:
    """The model for a thread = the model of the mailbox that owns the thread."""
    thread = (
        await session.execute(select(Thread).where(Thread.id == thread_id))
    ).scalar_one_or_none()
    return await resolve_for_account(session, thread.account_id if thread else None)
