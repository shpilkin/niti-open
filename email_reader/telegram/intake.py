"""Telegram intake: a message sent to a space's bot -> an item in that space's «Разобрать».

Deterministic, no model. Own text is a task to self (TASK); the date phrase is parsed
by the same quick-add parser as the board's box, so «до пятницы» means the same thing
on both entrances. A forwarded message is a request from its original sender — a
«нужен ответ» candidate with that person as the counterparty. Either way the item waits
in «Разобрать» (user_verdict IS NULL) until the user accepts, edits or dismisses it
(docs/ux-tasks.md §4.4, §7.3).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import quickadd, views
from email_reader.enums import ActionType, SourceKind
from email_reader.models import ActionItem

TITLE_LIMIT = 120


@dataclass(frozen=True)
class Draft:
    type: str
    title: str
    counterparty: str | None
    due: date | None
    reason: str | None


def _split(text: str) -> tuple[str, str]:
    """First non-empty line -> title; the rest -> reason (kept verbatim, whitespace
    collapsed per line)."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return "", ""
    head, rest = lines[0], " ".join(lines[1:])
    if len(head) > TITLE_LIMIT:
        rest = (head[TITLE_LIMIT:] + " " + rest).strip()
        head = head[:TITLE_LIMIT].rstrip()
    return head, rest


def classify(text: str, *, forwarded_from: str | None, today: date) -> Draft | None:
    """What a message should become. None when there is nothing to keep (empty text)."""
    head, rest = _split(text)
    if not head:
        return None
    if forwarded_from:
        reason = f"Переслано из Telegram, автор: {forwarded_from}"
        if rest:
            reason += f". {rest}"
        return Draft(
            type=ActionType.NEED_MY_REPLY, title=head, counterparty=forwarded_from,
            due=None, reason=reason,
        )
    title, due = quickadd.parse(head, today)
    return Draft(
        type=ActionType.TASK, title=title, counterparty=None, due=due,
        reason=f"Из Telegram. {rest}".strip() if rest else "Из Telegram",
    )


def origin_name(message) -> str | None:
    """The original author of a forwarded Telegram message, whatever shape the origin
    takes in aiogram 3 (user / hidden user / chat / channel). None when not forwarded."""
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    user = getattr(origin, "sender_user", None)
    if user is not None:
        name = " ".join(p for p in (getattr(user, "first_name", None),
                                    getattr(user, "last_name", None)) if p)
        handle = getattr(user, "username", None)
        return name or (f"@{handle}" if handle else None) or "Telegram"
    hidden = getattr(origin, "sender_user_name", None)
    if hidden:
        return hidden
    for attr in ("sender_chat", "chat"):
        chat = getattr(origin, attr, None)
        if chat is not None:
            return getattr(chat, "title", None) or getattr(chat, "username", None) or "Telegram"
    return "Telegram"


async def ingest(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    chat_id: int,
    message_id: int,
    text: str,
    forwarded_from: str | None,
    today: date,
) -> ActionItem | None:
    """Store the message as an unconfirmed item of the space's «Разобрать»."""
    draft = classify(text, forwarded_from=forwarded_from, today=today)
    if draft is None:
        return None
    return await views.create_task(
        session, title=draft.title, space_id=space_id, due=draft.due,
        type_=draft.type, counterparty=draft.counterparty,
        source_kind=SourceKind.TELEGRAM, source_ref=f"{chat_id}:{message_id}",
        reason=draft.reason, confirmed=False,
    )
