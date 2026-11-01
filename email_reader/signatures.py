"""Per-mailbox email signatures.

Each connected account (mailbox) can have a signature that is pre-filled into the
composer (new / reply / forward) and sent as part of the body. Stored in `app_settings`
under `signature:<account_id>`; absent/blank means "no signature". Scoped by account_id
— a signature is the sender's, so it stays with the mailbox even now that the boundary is
the space (docs/spaces.md §2).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import htmlview, settings_store
from email_reader.models import Account


def _key(account_id: uuid.UUID) -> str:
    return f"signature:{account_id}"


async def get_signature(session: AsyncSession, account_id: uuid.UUID | None) -> str:
    if account_id is None:
        return ""
    return (await settings_store.get_value(session, _key(account_id))) or ""


async def set_signature(
    session: AsyncSession, account_id: uuid.UUID, value: str
) -> None:
    # Signatures are rich HTML; sanitize before storing (also used in outgoing mail).
    clean = htmlview.sanitize_signature_html(value)
    await settings_store.set_value(session, _key(account_id), clean)


async def for_accounts(
    session: AsyncSession, accounts: list[Account]
) -> dict[str, str]:
    """Map str(account_id) -> signature, for embedding in the composer (JSON-friendly keys)."""
    out: dict[str, str] = {}
    for a in accounts:
        out[str(a.id)] = await get_signature(session, a.id)
    return out


def to_texts(sigs: dict[str, str]) -> dict[str, str]:
    """Plain-text versions of the signatures — pre-filled inline into the compose body
    (the rich HTML replaces the untouched text block at send time)."""
    from email_reader.htmlview import signature_to_text

    return {k: signature_to_text(v) for k, v in sigs.items()}


def to_fonts(sigs: dict[str, str]) -> dict[str, str]:
    """Шрифт поля ввода на ящик: письмо уходит шрифтом подписи, и набирать его надо им
    же — иначе человек видит одно, а получатель получает другое."""
    from email_reader.htmlview import font_of

    out: dict[str, str] = {}
    for key, html in sigs.items():
        face, size = font_of(html)
        if face:
            out[key] = f"font-family:{face};font-size:{size or '14px'}"
    return out
