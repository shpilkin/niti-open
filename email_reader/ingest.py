"""Bridge: ParsedEmail -> storage, with per-account dedup and provenance.

Dedup is PER-ACCOUNT (keyed on account + Message-ID, hash fallback): the same message
delivered to two of the user's mailboxes is stored once PER mailbox. This keeps mailboxes
strictly isolated — each account's emails/threads/projects are separate, so a per-mailbox
model only ever sees its own mailbox. Within one account, the same message in several
folders is still one email with multiple occurrences.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.enums import EventType
from email_reader.models import Email, EmailOccurrence
from email_reader.normalization import (
    EmailAddress,
    ParsedEmail,
    compute_dedup_hash,
    resolve_direction,
)
from email_reader.queue import enqueue
from email_reader.search import index as search_index


def _dump_addrs(addrs: list[EmailAddress]) -> list[dict]:
    return [a.model_dump() for a in addrs]


async def _find_existing(
    session: AsyncSession,
    account_id: uuid.UUID,
    rfc_message_id: str | None,
    dedup_hash: str,
) -> Email | None:
    # Dedup is PER-ACCOUNT: the same message in another mailbox is a separate email,
    # so a match in a different account does not count (strict mailbox isolation).
    if rfc_message_id:
        found = (
            await session.execute(
                select(Email).where(
                    Email.account_id == account_id,
                    Email.rfc_message_id == rfc_message_id,
                )
            )
        ).scalar_one_or_none()
        if found:
            return found
    return (
        await session.execute(
            select(Email).where(
                Email.account_id == account_id, Email.dedup_hash == dedup_hash
            )
        )
    ).scalar_one_or_none()


async def _ensure_occurrence(
    session: AsyncSession,
    email_id: uuid.UUID,
    account_id: uuid.UUID,
    folder: str,
    uid: int,
) -> None:
    exists = (
        await session.execute(
            select(EmailOccurrence).where(
                EmailOccurrence.account_id == account_id,
                EmailOccurrence.folder == folder,
                EmailOccurrence.uid == uid,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        session.add(
            EmailOccurrence(
                email_id=email_id, account_id=account_id, folder=folder, uid=uid
            )
        )


async def store_email(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    parsed: ParsedEmail,
    identities: set[str],
    folder: str | None = None,
    uid: int | None = None,
    normalize_plus_tags: bool = False,
) -> tuple[Email, bool]:
    """Idempotently store a parsed email. Returns (email, is_new).

    folder/uid record where the message lives on a server (provenance). They are
    omitted for a locally-composed sent copy — its occurrence is added later when
    the real message syncs back from the Sent folder (dedup by Message-ID).
    """
    has_occurrence = folder is not None and uid is not None
    direction = resolve_direction(
        parsed.from_.email, parsed.all_recipient_emails, identities, normalize_plus_tags
    )
    dedup_hash = compute_dedup_hash(
        parsed.from_.email,
        parsed.subject,
        parsed.sent_at.isoformat(),
        parsed.body_text,
    )

    existing = await _find_existing(session, account_id, parsed.rfc_message_id, dedup_hash)
    if existing is not None:
        # Same message in another folder of THIS account -> just record provenance.
        if has_occurrence:
            await _ensure_occurrence(session, existing.id, account_id, folder, uid)
        return existing, False

    email = Email(
        account_id=account_id,
        rfc_message_id=parsed.rfc_message_id,
        dedup_hash=dedup_hash,
        thread_id=None,  # assigned by the pipeline's threading step
        direction=str(direction),
        from_name=parsed.from_.name,
        from_addr=parsed.from_.email,
        to_addrs=_dump_addrs(parsed.to),
        cc_addrs=_dump_addrs(parsed.cc),
        subject=parsed.subject,
        body_text=parsed.body_text,
        body_raw_html=parsed.body_html,
        sent_at=parsed.sent_at,
        in_reply_to=parsed.in_reply_to,
        reference_ids=parsed.references,
        headers=parsed.headers,
        has_attachments=parsed.has_attachments,
    )
    session.add(email)
    await session.flush()  # assign email.id
    await search_index.index_mail(session, email)
    if has_occurrence:
        await _ensure_occurrence(session, email.id, account_id, folder, uid)
    await enqueue(session, EventType.EMAIL_STORED, {"email_id": str(email.id)})
    return email, True
