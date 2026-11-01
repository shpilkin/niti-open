"""The one-shot repair of bodies shredded by the old HTML-to-text pass."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from email_reader import settings_store
from email_reader.models import Email
from email_reader.pipeline.text_repair import (
    B64_DONE_KEY,
    DONE_KEY,
    repair_base64_bodies,
    repair_html_bodies,
)

NOW = datetime.now(UTC)

# What the old extraction (get_text("\n")) made of the HTML below, after clean_body.
SHREDDED = "e-mail:\n\nivanov\n@\ncorpmail\n.\nru"
HTML = (
    '<html><body><span>e-mail:</span> <span lang="EN-US">ivanov</span>@'
    '<span lang="EN-US">corpmail</span>.<span lang="EN-US">ru</span></body></html>'
)


async def _email(session, *, body, html):
    e = Email(
        direction="inbound", from_addr="a@b.c", to_addrs=[], cc_addrs=[], subject="S",
        body_text=body, body_raw_html=html, sent_at=NOW, dedup_hash=str(uuid.uuid4()),
    )
    session.add(e)
    await session.flush()
    return e.id


async def test_rebuilds_a_shredded_body(db):
    async with db() as s:
        eid = await _email(s, body=SHREDDED, html=HTML)
        stats = await repair_html_bodies(s)
        assert stats == {"scanned": 1, "rebuilt": 1}
        assert await s.scalar(select(Email.body_text).where(Email.id == eid)) == (
            "e-mail: ivanov@corpmail.ru"
        )


async def test_leaves_a_real_text_part_alone(db):
    """A multipart letter carries its own text/plain; only the HTML twin was shredded,
    and re-deriving from HTML would throw the better source away."""
    async with db() as s:
        eid = await _email(s, body="Написано человеком.", html=HTML)
        stats = await repair_html_bodies(s)
        assert stats == {"scanned": 1, "rebuilt": 0}
        assert await s.scalar(select(Email.body_text).where(Email.id == eid)) == (
            "Написано человеком."
        )


async def test_second_pass_touches_nothing(db):
    async with db() as s:
        await _email(s, body=SHREDDED, html=HTML)
        await repair_html_bodies(s)
        assert await settings_store.get_value(s, DONE_KEY) == "1"
        assert await repair_html_bodies(s) == {"scanned": 0, "rebuilt": 0}


async def test_rescan_after_an_interrupted_pass_is_a_no_op(db):
    """The flag is set at the end; a quit halfway leaves it unset, so the next start
    rescans — and must not touch what it already rebuilt."""
    async with db() as s:
        eid = await _email(s, body=SHREDDED, html=HTML)
        await repair_html_bodies(s)
        await settings_store.delete_value(s, DONE_KEY)
        assert await repair_html_bodies(s) == {"scanned": 1, "rebuilt": 0}
        assert await s.scalar(select(Email.body_text).where(Email.id == eid)) == (
            "e-mail: ivanov@corpmail.ru"
        )


# --- Letters whose text is the base64 of an attached .eml ---

ATTACHED_LETTER = base64.b64encode(
    b"Received: from mail.yandex.net\r\nFrom: someone@else.ru\r\n"
    b"Subject: forwarded\r\n\r\n" + b"chuzhoe pismo " * 20
).decode()
REAL_HTML = "<div><p>Коллеги, добрый день!</p><p>Смета во вложении.</p></div>"


async def test_rebuilds_a_body_that_is_an_attached_letter(db):
    async with db() as s:
        eid = await _email(s, body=ATTACHED_LETTER, html=REAL_HTML)
        assert await repair_base64_bodies(s) == {"scanned": 1, "rebuilt": 1}
        assert await s.scalar(select(Email.body_text).where(Email.id == eid)) == (
            "Коллеги, добрый день!\n\nСмета во вложении."
        )


async def test_latin_prose_is_not_mistaken_for_base64(db):
    """Shape alone is not enough — a long run of latin letters matches it. What decides
    is that the decoded bytes are a mail message."""
    text = "Please confirm the quarterly budget review meeting agenda with the team " * 5
    async with db() as s:
        eid = await _email(s, body=text, html=REAL_HTML)
        assert await repair_base64_bodies(s) == {"scanned": 1, "rebuilt": 0}
        assert await s.scalar(select(Email.body_text).where(Email.id == eid)) == text


async def test_a_token_that_decodes_but_is_not_a_letter_is_left_alone(db):
    """A JWT is valid base64 of 200+ chars; its bytes are not a message."""
    token = base64.b64encode(b"x" * 400).decode()
    async with db() as s:
        eid = await _email(s, body=token, html=REAL_HTML)
        assert await repair_base64_bodies(s) == {"scanned": 1, "rebuilt": 0}
        assert await s.scalar(select(Email.body_text).where(Email.id == eid)) == token


async def test_base64_pass_runs_once(db):
    async with db() as s:
        await _email(s, body=ATTACHED_LETTER, html=REAL_HTML)
        await repair_base64_bodies(s)
        assert await settings_store.get_value(s, B64_DONE_KEY) == "1"
        assert await repair_base64_bodies(s) == {"scanned": 0, "rebuilt": 0}
