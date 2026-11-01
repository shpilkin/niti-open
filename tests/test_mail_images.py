"""Картинки в письме: вложенные в само письмо видны сразу, из интернета — по кнопке.

Встроенная картинка — <img src="cid:image001.png@01DD4CD3.5381F860>: её байты лежат в
самом письме. Раньше очистка HTML вырезала такой адрес, а кнопка «Показать картинки»
разрешала только интернет: встроенные картинки не показывались никогда.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from email.message import EmailMessage

import pytest
from fastapi import Request

import email_reader.api.app  # noqa: F401 — регистрирует фильтры шаблонов
from email_reader import attachments as attachments_svc
from email_reader import db as db_mod
from email_reader import htmlview
from email_reader.api.routers import mail as mail_router
from email_reader.enums import Direction
from email_reader.models import Account, Email, EmailOccurrence
from tests.conftest import SPACE_ID, mk_space

PNG = b"\x89PNG\r\n\x1a\n" + b"picture"
CID = "image001.png@01DD4CD3.5381F860"
# Так Exchange присылает приглашение на встречу: разметка без единого слова.
INVITE = ('<html><head><meta name="Generator" content="Microsoft Exchange Server">'
          '<!-- converted from rtf --></head><body><font face="Times New Roman" size="3">'
          '<span style="font-size:12pt;"><a name="BM_BEGIN"></a></span></font></body></html>')


def _request() -> Request:
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "path": "/email/x",
        "raw_path": b"/email/x", "query_string": b"", "headers": [], "client": ("t", 1),
        "server": ("t", 80), "scheme": "http", "root_path": "", "app": None,
    }
    return Request(scope)


@pytest.fixture
def wired(db, monkeypatch):
    monkeypatch.setattr(db_mod, "_sessionmaker", db)
    return db


async def _letter(s, html: str, *, provider: str = "yandex") -> Email:
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name=uuid.uuid4().hex, provider=provider, host="h", port=993,
                  login="me@corp.ru", password_enc="x", folders=["INBOX"])
    s.add(acc)
    await s.flush()
    email = Email(account_id=acc.id, dedup_hash=uuid.uuid4().hex, direction=Direction.INBOUND,
                  from_addr="boss@corp.ru", to_addrs=[], cc_addrs=[], subject="Смета",
                  body_raw_html=html, sent_at=datetime.now(UTC))
    s.add(email)
    await s.flush()
    s.add(EmailOccurrence(email_id=email.id, account_id=acc.id, folder="INBOX", uid=7))
    await s.commit()
    return email


async def _frame(email_id, images: int = 0) -> str:
    return (await mail_router.email_html(email_id, images)).body.decode()


async def test_a_picture_inside_an_imap_letter_is_taken_from_the_letter_once(
    wired, monkeypatch
):
    async with wired() as s:
        email = await _letter(s, '<p>Смета</p><img src="cid:logo@corp">', provider="yandex")
    msg = EmailMessage()
    msg.set_content("Смета")
    msg.add_related(PNG, maintype="image", subtype="png", cid="<logo@corp>")
    calls: list[int] = []

    async def fetch_raw(**kw):
        calls.append(kw["uid"])
        return msg.as_bytes()

    monkeypatch.setattr(attachments_svc, "fetch_raw", fetch_raw)
    monkeypatch.setattr(attachments_svc, "resolve_password", lambda _acc: "pw")

    first, again = await _frame(email.id), await _frame(email.id)

    assert 'src="data:image/png;base64,' in first and first == again
    assert calls == [7]  # сервер спрошен один раз, дальше — из кэша письма


async def test_an_unreachable_server_is_not_asked_again_at_every_open(wired, monkeypatch):
    """Без сети каждое открытие письма ждало таймаут IMAP, прежде чем показать текст."""
    async with wired() as s:
        email = await _letter(s, '<p>Смета</p><img src="cid:logo@corp">', provider="yandex")
    calls: list[int] = []

    async def offline(**kw):
        calls.append(kw["uid"])
        raise TimeoutError("нет сети")

    monkeypatch.setattr(attachments_svc, "fetch_raw", offline)
    monkeypatch.setattr(attachments_svc, "resolve_password", lambda _acc: "pw")

    first, again = await _frame(email.id), await _frame(email.id)

    assert "Смета" in first and "Смета" in again  # текст показывается и без картинки
    assert calls == [7]


async def test_the_picture_button_is_there_only_for_pictures_from_the_internet(wired):
    async with wired() as s:
        inside = await _letter(s, f'<p>Смета</p><img src="cid:{CID}">')
    async with wired() as s:
        outside = await _letter(s, '<p>Смета</p><img src="https://track.example/p.png">')

    body_in = (await mail_router.email_pane(_request(), inside.id, back="")).body.decode()
    body_out = (await mail_router.email_pane(_request(), outside.id, back="")).body.decode()

    assert "Показать картинки" not in body_in
    assert "Показать картинки" in body_out
    assert "img-src data:;" in (await mail_router.email_html(outside.id, 0)).headers[
        "Content-Security-Policy"
    ]  # из интернета — только по кнопке, как и было


async def test_an_invitation_without_words_reads_as_a_letter_without_text(wired):
    """Пустая рамка под кнопкой картинок выглядела поломкой: картинок там нет, текста тоже."""
    async with wired() as s:
        email = await _letter(s, INVITE)

    body = (await mail_router.email_pane(_request(), email.id, back="")).body.decode()

    assert "mailframe-" not in body and "Показать картинки" not in body
    assert "Письмо без текста." in body


def test_a_data_address_passes_only_as_a_picture():
    """data: в <img> — картинка; data: в ссылке — целая страница, её не пропускаем."""
    got = htmlview.sanitize_email_html(
        '<img src="data:image/gif;base64,R0lGOD"><a href="data:text/html,x">ссылка</a>'
        '<img src="data:text/html;base64,PHNjcmlwdD4=">'
    )
    assert 'src="data:image/gif;base64,R0lGOD"' in got
    assert "data:text" not in got
