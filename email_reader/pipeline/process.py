"""EmailStored handler: clean -> triage -> thread -> enqueue ThreadReady."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.enums import EventType
from email_reader.labels import apply_rules_to_email, ignored_by_rule
from email_reader.models import Email
from email_reader.pipeline.cleaning import clean_body
from email_reader.pipeline.threading import assign_thread
from email_reader.pipeline.triage import triage
from email_reader.queue import enqueue


async def process_email_stored(session: AsyncSession, payload: dict) -> None:
    email_id = uuid.UUID(payload["email_id"])
    email = (
        await session.execute(select(Email).where(Email.id == email_id))
    ).scalar_one_or_none()
    if email is None:
        return

    # Clean body in place (extraction reads body_text); raw HTML kept for "show original".
    email.body_text = clean_body(email.body_text, email.body_raw_html)

    is_low, _reason = triage(email.headers, email.from_addr)
    email.is_low_value = is_low

    thread = await assign_thread(session, email)

    # Deterministic sender-rule labels (correspondent -> label).
    await apply_rules_to_email(session, email)

    # A sender the user asked to ignore: low value like triage'd bulk mail, no model call.
    if not is_low and await ignored_by_rule(session, email):
        email.is_low_value = True
        is_low = True
    if not is_low:
        await enqueue(session, EventType.THREAD_READY, {"thread_id": str(thread.id)})
