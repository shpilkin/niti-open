"""Черновики писем: недописанное письмо переживает закрытие композера.

Композер сохраняет себя сам, пока человек печатает, и при закрытии. Черновик живёт до
отправки — она его и удаляет. Вложения не хранятся: файлы приходят в форме только при
отправке, автосохранение их не видит.

Не путать с `drafts.py`: там ответ, который предлагает модель, здесь — текст человека.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.models import Draft


def _uuid_or_none(raw: str | uuid.UUID | None) -> uuid.UUID | None:
    if isinstance(raw, uuid.UUID):
        return raw
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        return None


def has_content(to: str, cc: str, subject: str, body: str) -> bool:
    return any(part.strip() for part in (to, cc, subject, body))


async def get(session: AsyncSession, draft_id: str | uuid.UUID) -> Draft | None:
    did = _uuid_or_none(draft_id)
    return await session.get(Draft, did) if did else None


async def save(
    session: AsyncSession, *, draft_id: str, account_id: uuid.UUID, to: str, cc: str,
    subject: str, body: str, in_reply_to: str, references: str, forward_email_id: str,
    back_url: str,
) -> Draft | None:
    """Создаёт черновик или обновляет существующий.

    Если `draft_id` указан, а строки нет, — черновик уже отправлен или удалён, и
    сохранение ничего не воскрешает: отложенное автосохранение приходит и после этого.
    """
    draft = await get(session, draft_id) if draft_id else None
    if draft is None:
        if draft_id or not has_content(to, cc, subject, body):
            return None
        draft = Draft(account_id=account_id)
        session.add(draft)
    draft.account_id = account_id
    draft.to_line = to
    draft.cc_line = cc
    draft.subject = subject
    draft.body = body
    draft.in_reply_to = in_reply_to
    draft.references = references
    draft.forward_email_id = _uuid_or_none(forward_email_id)
    draft.back_url = back_url
    await session.flush()
    return draft


async def discard(session: AsyncSession, draft_id: str | uuid.UUID) -> bool:
    did = _uuid_or_none(draft_id)
    if did is None:
        return False
    res = await session.execute(delete(Draft).where(Draft.id == did))
    return bool(res.rowcount)


async def for_account(session: AsyncSession, account_id: uuid.UUID) -> list[Draft]:
    return list((await session.execute(
        select(Draft).where(Draft.account_id == account_id)
        .order_by(Draft.updated_at.desc())
    )).scalars())


async def counts_by_account(session: AsyncSession) -> dict[uuid.UUID, int]:
    """Сколько черновиков в каждом ящике — для папки в боковом меню."""
    rows = (await session.execute(
        select(Draft.account_id, func.count()).group_by(Draft.account_id)
    )).all()
    return {account_id: n for account_id, n in rows}
