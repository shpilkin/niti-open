from __future__ import annotations

from email_reader.mailer import build_message, resolve_smtp
from email_reader.models import Account


def test_build_message_basic():
    msg = build_message("me@x.com", "you@y.com", "Привет", "Текст письма")
    assert msg["From"] == "me@x.com"
    assert msg["To"] == "you@y.com"
    assert msg["Subject"] == "Привет"
    assert msg["Message-ID"]
    assert "Текст письма" in msg.get_content()


def test_build_message_threading_headers():
    msg = build_message(
        "me@x.com", "you@y.com", "Re: тема", "ответ",
        in_reply_to="<a@x>", references=["<root@x>", "<a@x>"],
    )
    assert msg["In-Reply-To"] == "<a@x>"
    # references preserved, in_reply_to not duplicated
    assert msg["References"] == "<root@x> <a@x>"


def test_build_message_appends_in_reply_to_to_references():
    msg = build_message("m@x", "y@y", "s", "b", in_reply_to="<new@x>", references=["<old@x>"])
    assert msg["References"] == "<old@x> <new@x>"


def test_build_message_with_attachments():
    msg = build_message(
        "me@x.com", "you@y.com", "С файлом", "Смотри вложение",
        attachments=[
            {"name": "отчёт.pdf", "mime": "application/pdf", "bytes": b"%PDF-1.4 data"},
            {"name": "note.txt", "mime": "text/plain", "bytes": b"hello"},
        ],
    )
    parts = list(msg.iter_attachments())
    names = {p.get_filename() for p in parts}
    assert names == {"отчёт.pdf", "note.txt"}
    pdf = next(p for p in parts if p.get_filename() == "отчёт.pdf")
    assert pdf.get_content_type() == "application/pdf"
    assert pdf.get_payload(decode=True) == b"%PDF-1.4 data"


def test_build_message_bad_mime_falls_back():
    # A missing/garbage mime must not crash message building.
    msg = build_message(
        "me@x.com", "you@y.com", "s", "b",
        attachments=[{"name": "x.bin", "mime": None, "bytes": b"\x00\x01"}],
    )
    att = next(iter(msg.iter_attachments()))
    assert att.get_filename() == "x.bin"
    assert att.get_payload(decode=True) == b"\x00\x01"


def test_build_message_html_alternative():
    msg = build_message(
        "me@x.com", "you@y.com", "S", "привет\nстрока",
        html_body="<div>привет<br>строка</div><br>--<br>"
                  "<span style=\"color:#e11\">Подпись</span>",
    )
    assert msg.get_content_type() == "multipart/alternative"
    types = {p.get_content_type() for p in msg.walk()}
    assert "text/plain" in types and "text/html" in types
    html = next(p for p in msg.walk() if p.get_content_type() == "text/html").get_content()
    assert "color:#e11" in html  # styled signature survives into the HTML part
    text = next(p for p in msg.walk() if p.get_content_type() == "text/plain").get_content()
    assert "привет" in text


def test_resolve_smtp_from_account():
    acc = Account(name="a", provider="imap", host="imap.x.com", login="l",
                  smtp_host="smtp.x.com", smtp_port=465)
    assert resolve_smtp(acc) == ("smtp.x.com", 465)


def test_resolve_smtp_from_provider_preset():
    acc = Account(name="g", provider="gmail", host="imap.gmail.com", login="l",
                  smtp_host=None, smtp_port=465)
    assert resolve_smtp(acc) == ("smtp.gmail.com", 465)


def test_resolve_smtp_derives_from_imap_host():
    acc = Account(name="o", provider="imap", host="imap.corp.ru", login="l",
                  smtp_host=None, smtp_port=465)
    assert resolve_smtp(acc) == ("smtp.corp.ru", 465)
