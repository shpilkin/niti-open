"""A letter attached to a letter is a file, not a body part."""

from __future__ import annotations

import base64

from email_reader.attachments import parse_attachments

INNER = (
    b"From: someone@else.ru\r\n"
    b"Subject: =?utf-8?B?0J7QsdC90L7QstC70LXQvdC40Y8=?=\r\n"  # «Обновления»
    b"Content-Type: text/plain\r\n\r\nchuzhoe pismo\r\n"
)


def _letter(*, cte: str, disposition: str = "") -> bytes:
    payload = base64.b64encode(INNER).decode() if cte == "base64" else INNER.decode()
    return (
        "From: olga@x.ru\r\nTo: me@y.ru\r\nSubject: Смета\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/html\r\n\r\n<p>Коллеги, добрый день!</p>\r\n"
        f"--B\r\nContent-Type: message/rfc822\r\nContent-Transfer-Encoding: {cte}\r\n"
        f"{disposition}\r\n{payload}\r\n"
        "--B\r\nContent-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n"
        'Content-Disposition: attachment; filename="smeta.pdf"\r\n\r\n'
        f"{base64.b64encode(b'%PDF-1.4').decode()}\r\n--B--\r\n"
    ).encode()


def test_attached_letter_is_offered_as_a_file():
    """It is excluded from the body on purpose, so losing it here loses it entirely."""
    got = parse_attachments(
        _letter(
            cte="base64",
            disposition='Content-Disposition: attachment; filename="pismo.eml"\r\n',
        )
    )
    assert [a["name"] for a in got] == ["pismo.eml", "smeta.pdf"]
    letter = next(a for a in got if a["name"] == "pismo.eml")
    assert letter["mime"] == "message/rfc822"
    assert b"chuzhoe pismo" in letter["bytes"]  # decoded, not the base64 we received


def test_attached_letter_without_a_filename_is_named_after_its_subject():
    got = parse_attachments(_letter(cte="7bit"))
    assert got[0]["name"] == "Обновления.eml"


def test_other_attachments_are_untouched():
    got = parse_attachments(_letter(cte="base64"))
    pdf = next(a for a in got if a["name"] == "smeta.pdf")
    assert pdf["bytes"] == b"%PDF-1.4"


# --- Пересылка не должна молча терять вложения оригинала ---------------------------------
import json  # noqa: E402
import uuid  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

import pytest  # noqa: E402

from email_reader import accounts as accounts_svc  # noqa: E402
from email_reader import attachments as attachments_svc  # noqa: E402
from email_reader.models import Email, EmailOccurrence  # noqa: E402
from tests.conftest import mk_space  # noqa: E402


async def _acc_and_email(s):
    """Ящик и входящее письмо с вложением — как их видит пересылка."""
    space = await mk_space(s)
    acc = await accounts_svc.create_account(
        s, name="Я", provider="yandex", host="imap.yandex.ru", port=993,
        login="me@ya.ru", password="secret", folders=["INBOX"], space_id=space.id,
    )
    email = Email(
        account_id=acc.id, direction="inbound", from_addr="olga@x.ru", from_name="Ольга",
        to_addrs=[], cc_addrs=[], subject="Расписание", body_text="во вложении",
        sent_at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC), has_attachments=True,
        dedup_hash=str(uuid.uuid4()),
    )
    s.add(email)
    await s.flush()
    s.add(EmailOccurrence(email_id=email.id, account_id=acc.id, folder="INBOX", uid=7))
    await s.flush()
    return acc, email


def _index(email_id, entries):
    cache = attachments_svc._cache_dir(email_id)
    cache.mkdir(parents=True, exist_ok=True)
    for e in entries:
        if e.get("file"):
            (cache / e["file"]).write_bytes(b"data")
    (cache / "_index.json").write_text(json.dumps(entries))


@pytest.mark.asyncio
async def test_forwarding_takes_the_files_that_are_here(db):
    async with db() as s:
        acc, email = await _acc_and_email(s)
        _index(email.id, [{"name": "смета.pdf", "mime": "application/pdf", "file": "0.bin"}])
        ready, missing, unknown = await attachments_svc.for_forwarding(s, email.id)
    assert [f["name"] for f in ready] == ["смета.pdf"]
    assert missing == [] and unknown is False


