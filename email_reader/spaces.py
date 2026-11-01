"""Spaces: the context boundary above mailboxes (docs/spaces.md).

A space («Работа», «Личное») owns its sources — mailboxes (Account), Telegram bots
(TelegramBot), meetings — and everything computed from them: boards, dossiers, projects,
search. Between spaces nothing commingles; inside one, everything does. The space also
owns what used to sit on the mailbox: the model, the prompts, the profile.

There is always at least one space: the first mailbox, bot or meeting to arrive creates
«Основное» when nothing else exists, so no code path ever has to handle «no space».
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.crypto import decrypt, encrypt
from email_reader.models import Account, ActionItem, Meeting, Project, Space, TelegramBot

DEFAULT_NAME = "Основное"

_UNSET: object = object()


async def list_spaces(session: AsyncSession) -> list[Space]:
    rows = await session.execute(select(Space).order_by(Space.sort, Space.created_at, Space.name))
    return list(rows.scalars().all())


async def get_space(session: AsyncSession, space_id: uuid.UUID) -> Space | None:
    return (await session.execute(select(Space).where(Space.id == space_id))).scalar_one_or_none()


async def ensure_default(session: AsyncSession) -> Space:
    """The space to use when nobody said which: the first one, created if none exists."""
    rows = await list_spaces(session)
    if rows:
        return rows[0]
    space = Space(name=DEFAULT_NAME)
    session.add(space)
    await session.flush()
    return space


async def resolve(session: AsyncSession, raw: str | None) -> Space:
    """The space a request works in: the cookie's, if it still exists, else the default."""
    if raw:
        try:
            found = await get_space(session, uuid.UUID(raw))
        except ValueError:
            found = None
        if found is not None:
            return found
    return await ensure_default(session)


async def create_space(
    session: AsyncSession, name: str, model_connection_id: uuid.UUID | None = None
) -> Space:
    n = (await session.execute(select(func.count()).select_from(Space))).scalar_one()
    space = Space(name=name.strip(), sort=n, model_connection_id=model_connection_id)
    session.add(space)
    await session.flush()
    # Доски — часть устройства пространства (docs/ux-tasks.md §17.5), а не разовая миграция.
    from email_reader import boards as boards_svc

    await boards_svc.seed(session, space.id)
    return space


async def update_space(
    session: AsyncSession,
    space_id: uuid.UUID,
    *,
    name: str | None = None,
    model_connection_id: uuid.UUID | None | object = _UNSET,
) -> Space | None:
    space = await get_space(session, space_id)
    if space is None:
        return None
    if name is not None and name.strip():
        space.name = name.strip()
    if model_connection_id is not _UNSET:
        space.model_connection_id = model_connection_id
    return space


async def delete_blocker(session: AsyncSession, space_id: uuid.UUID) -> str | None:
    """Why the space cannot be deleted, or None. Everything inside points at the space
    with a NOT NULL key, so an occupied space is never deleted — moved out of first.
    Soft-deleted meetings are not «inside» to the user (nothing shows them, nothing can
    move them), so they don't count; delete_space drops their rows with the space."""
    for model, what in (
        (Account, "ящики"), (TelegramBot, "Telegram-боты"), (Project, "проекты"),
        (Meeting, "встречи"), (ActionItem, "обязательства"),
    ):
        q = select(func.count()).select_from(model).where(model.space_id == space_id)
        if model is Meeting:
            q = q.where(Meeting.deleted_at.is_(None))
        n = (await session.execute(q)).scalar_one()
        if n:
            return f"В пространстве ещё есть {what} ({n}) — сначала перенесите или удалите их."
    if len(await list_spaces(session)) <= 1:
        return "Последнее пространство удалить нельзя."
    return None


async def delete_space(session: AsyncSession, space_id: uuid.UUID) -> str | None:
    """Delete an empty space. Returns the blocker text when it is not empty."""
    blocker = await delete_blocker(session, space_id)
    if blocker:
        return blocker
    space = await get_space(session, space_id)
    if space is not None:
        await session.execute(
            delete(Meeting).where(Meeting.space_id == space_id, Meeting.deleted_at.is_not(None))
        )
        await session.delete(space)
        await session.flush()
    return None


async def space_of_account(session: AsyncSession, account_id: uuid.UUID | None) -> uuid.UUID | None:
    if account_id is None:
        return None
    return (
        await session.execute(select(Account.space_id).where(Account.id == account_id))
    ).scalar_one_or_none()


def account_ids_of(space_id: uuid.UUID):
    """Subquery: ids of the mailboxes in a space — for scoping mail by space."""
    return select(Account.id).where(Account.space_id == space_id)


# --- Telegram bots -------------------------------------------------------------------


def resolve_bot_token(bot: TelegramBot) -> str:
    return decrypt(bot.token_enc) if bot.token_enc else ""


async def list_bots(session: AsyncSession, space_id: uuid.UUID | None = None) -> list[TelegramBot]:
    q = select(TelegramBot).order_by(TelegramBot.created_at)
    if space_id is not None:
        q = q.where(TelegramBot.space_id == space_id)
    return list((await session.execute(q)).scalars().all())


async def active_bots(session: AsyncSession) -> list[TelegramBot]:
    """Enabled bots — one is polled per row."""
    q = select(TelegramBot).where(TelegramBot.enabled.is_(True)).order_by(TelegramBot.created_at)
    return list((await session.execute(q)).scalars().all())


async def get_bot(session: AsyncSession, bot_id: uuid.UUID) -> TelegramBot | None:
    return (
        await session.execute(select(TelegramBot).where(TelegramBot.id == bot_id))
    ).scalar_one_or_none()


async def create_bot(
    session: AsyncSession, space_id: uuid.UUID, *, name: str, token: str,
    chat_id: int | None = None,
) -> TelegramBot:
    bot = TelegramBot(
        space_id=space_id, name=name.strip() or "Telegram", token_enc=encrypt(token.strip()),
        chat_id=chat_id, enabled=True,
    )
    session.add(bot)
    await session.flush()
    return bot


async def update_bot(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    name: str | None = None,
    token: str | None = None,  # blank/None = keep existing
    chat_id: int | None | object = _UNSET,
    enabled: bool | None = None,
) -> TelegramBot | None:
    bot = await get_bot(session, bot_id)
    if bot is None:
        return None
    if name is not None and name.strip():
        bot.name = name.strip()
    if token and token.strip():
        bot.token_enc = encrypt(token.strip())
    if chat_id is not _UNSET:
        bot.chat_id = chat_id
    if enabled is not None:
        bot.enabled = enabled
    return bot


async def delete_bot(session: AsyncSession, bot_id: uuid.UUID) -> None:
    bot = await get_bot(session, bot_id)
    if bot is not None:
        await session.delete(bot)
        await session.flush()
