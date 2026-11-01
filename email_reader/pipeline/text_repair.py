"""One-shot repair of message bodies shredded by the old HTML-to-text pass.

Until the block/inline fix in `cleaning.html_to_text`, every text node was joined with a
newline, so Outlook mail — where one sentence is a chain of `<span lang="EN-US">` — was
stored as one word per line. New mail is clean now; mail already in the database is not,
because ingest overwrites `body_text` in place and never returns to the raw HTML.

A row is rebuilt only when its stored `body_text` is byte-for-byte what the OLD code
would have produced from that row's own `body_raw_html`. Nothing else distinguishes a
shredded body from a genuine text/plain part, and the plain part must be left alone.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import email
import re
import uuid

from bs4 import BeautifulSoup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import settings_store
from email_reader.models import Email
from email_reader.pipeline.cleaning import clean_body

DONE_KEY = "text_repair.html_bodies_v1"
B64_DONE_KEY = "text_repair.b64_bodies_v1"


def _legacy_html_to_text(html: str) -> str:
    """The pre-fix extraction, frozen. Never used to produce text — only to recognise
    text it produced."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    return soup.get_text("\n")


def rebuilt_body(stored: str | None, html: str | None) -> str | None:
    """Corrected text, or None if this row did not come from the old extraction."""
    if not html:
        return None
    if (stored or "") != clean_body(_legacy_html_to_text(html), None):
        return None
    fixed = clean_body(None, html)
    return fixed if fixed != (stored or "") else None


def _rebuild_batch(rows: list[tuple[uuid.UUID, str | None, str | None]]):
    return [(eid, rebuilt_body(text, html)) for eid, text, html in rows]


async def _run_pass(
    session: AsyncSession, done_key: str, rebuild, *, batch: int
) -> dict[str, int]:
    """One pass over every letter that kept its raw HTML. Commits per batch: parsing
    400 MB of stored HTML takes minutes, and a quit halfway through should keep what it
    managed — the flag is set only at the end, so the next start finishes the job."""
    stats = {"scanned": 0, "rebuilt": 0}
    if await settings_store.get_value(session, done_key):
        return stats

    ids = list(
        (
            await session.execute(
                select(Email.id).where(
                    Email.body_raw_html.is_not(None), Email.body_raw_html != ""
                )
            )
        ).scalars()
    )

    for start in range(0, len(ids), batch):
        rows = (
            await session.execute(
                select(Email.id, Email.body_text, Email.body_raw_html).where(
                    Email.id.in_(ids[start : start + batch])
                )
            )
        ).all()
        # Off the event loop: this is BeautifulSoup on megabytes, and the same process
        # serves the UI.
        for email_id, fixed in await asyncio.to_thread(rebuild, list(rows)):
            stats["scanned"] += 1
            if fixed is not None:
                await session.execute(
                    update(Email).where(Email.id == email_id).values(body_text=fixed)
                )
                stats["rebuilt"] += 1
        await session.commit()

    await settings_store.set_value(session, done_key, "1")
    await session.commit()
    return stats


async def repair_html_bodies(session: AsyncSession, *, batch: int = 100) -> dict[str, int]:
    """Rewrite every shredded body once."""
    return await _run_pass(session, DONE_KEY, _rebuild_batch, batch=batch)


# --- Letters whose text is the base64 of an attached .eml ---

# Real base64 bodies wrap by line and hold nothing else; prose with spaces is not this.
_B64_BODY_RE = re.compile(r"^[A-Za-z0-9+/=\r\n]{200,}$")
_MAIL_HEADERS = ("from", "received", "content-type", "subject", "message-id")


def is_attached_letter_base64(text: str | None) -> bool:
    """True only for the payload of an attached message: base64 that decodes strictly and
    turns out to be a mail message. A JWT or a dump of latin words passes the shape test —
    the headers of the decoded message are what makes this safe."""
    if not text or not _B64_BODY_RE.match(text):
        return False
    try:
        decoded = base64.b64decode(re.sub(r"[\r\n]", "", text), validate=True)
    except (binascii.Error, ValueError):
        return False
    return any(email.message_from_bytes(decoded).get(h) for h in _MAIL_HEADERS)


def _rebuilt_b64_batch(rows: list[tuple[uuid.UUID, str | None, str | None]]):
    out = []
    for eid, text, html in rows:
        fixed = clean_body(None, html) if html and is_attached_letter_base64(text) else None
        out.append((eid, fixed or None))
    return out


async def repair_base64_bodies(session: AsyncSession, *, batch: int = 100) -> dict[str, int]:
    """Put the letter's own text back where the parser stored an attached .eml instead.

    Fixed at the source in normalization.parser (_own_parts); this only reaches the mail
    that arrived before the fix. The value written is what the parser produces now."""
    return await _run_pass(session, B64_DONE_KEY, _rebuilt_b64_batch, batch=batch)
