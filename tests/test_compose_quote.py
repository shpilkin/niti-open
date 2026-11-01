"""Цитата в ответе и пересылке: переписка должна остаться собой."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from email_reader import accounts as accounts_svc
from email_reader import compose
from email_reader.compose import _quote_block
from email_reader.enums import Direction
from email_reader.mailer import build_message
from email_reader.models import Email, Thread

ORIGINAL_HTML = (
    '<div style="font-family:Calibri;font-size:11pt">'
    "<p>Смотри таблицу:</p>"
    '<table border="1"><tr><td>Квартал</td><td>План</td></tr>'
    "<tr><td>4Q</td><td>120</td></tr></table>"
    '<img src="cid:image001.png@01D.9" width="120">'
    "</div>"
)


def _capture(monkeypatch) -> dict:
    got: dict = {}

    async def fake_send_mail(account, password, *, to, subject, body, html_body=None,
                             cc="", **kw):
        got["text"], got["html"] = body, html_body or ""
        return build_message(account.login, to, subject, body, html_body=html_body, cc=cc)

    monkeypatch.setattr(compose, "send_mail", fake_send_mail)
    return got


async def _account_and_letter(s) -> tuple:
    acc = await accounts_svc.create_account(
        s, name="Y", provider="yandex", host="imap.yandex.ru", port=993,
        login="me@y.ru", password="secret", folders=["INBOX", "Sent"],
    )
    th = Thread(account_id=acc.id)
    s.add(th)
    await s.flush()
    orig = Email(
        account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
        direction=Direction.INBOUND, from_addr="boss@corp.ru", from_name="Босс",
        to_addrs=[{"email": "me@y.ru"}], cc_addrs=[], subject="Планы",
        rfc_message_id="<orig@corp.ru>", body_text="Смотри таблицу:\nКвартал План\n4Q 120",
        body_raw_html=ORIGINAL_HTML, sent_at=datetime(2026, 6, 2, 10, 0, tzinfo=UTC),
    )
    s.add(orig)
    await s.commit()
    return acc, orig


async def test_an_untouched_reply_quote_keeps_the_original_table(db, monkeypatch):
    """Цитата уходила экранированным текстом: таблица рассыпалась в строки, шрифт
    оригинала пропадал. Теперь в HTML-часть идёт сам HTML оригинала."""
    got = _capture(monkeypatch)
    async with db() as s:
        acc, orig = await _account_and_letter(s)
        body = "Согласен." + _quote_block(orig, "reply")
        result = await compose.send(
            s, account_id=acc.id, to="boss@corp.ru", subject="Re: Планы", body=body,
            in_reply_to="<orig@corp.ru>", references=[],
        )
    assert result.error is None
    html = got["html"]
    assert "<table" in html and "&lt;table" not in html
    assert "font-family:Calibri" in html
    assert "<blockquote" in html          # видно, где чужое
    assert "cid:image001" not in html     # вложения оригинала с письмом не уходят
    assert "Квартал" in got["text"]       # текстовая часть по-прежнему цитирует текстом


async def test_an_edited_quote_goes_as_text(db, monkeypatch):
    """Правленую цитату подменять оригиналом нельзя: уйдёт не то, что человек видел."""
    got = _capture(monkeypatch)
    async with db() as s:
        acc, orig = await _account_and_letter(s)
        body = "Согласен." + _quote_block(orig, "reply").replace("120", "999")
        result = await compose.send(
            s, account_id=acc.id, to="boss@corp.ru", subject="Re: Планы", body=body,
            in_reply_to="<orig@corp.ru>", references=[],
        )
    assert result.error is None
    assert "<table" not in got["html"]
    assert "999" in got["html"] and "999" in got["text"]


async def test_a_forward_keeps_the_original_too(db, monkeypatch):
    got = _capture(monkeypatch)
    async with db() as s:
        acc, orig = await _account_and_letter(s)
        body = "Держи." + _quote_block(orig, "forward")
        result = await compose.send(
            s, account_id=acc.id, to="x@x.com", subject="Fwd: Планы", body=body,
            in_reply_to=None, references=[], forward_email_id=orig.id,
        )
    assert result.error is None
    assert "<table" in got["html"]
    assert "Тема: Планы" in got["html"]


OUTLOOK_HTML = (
    "<style><!--\n"
    "/* Style Definitions */\n"
    "@font-face {font-family:Calibri; panose-1:2 15 5 2;}\n"
    "p.MsoNormal, li.MsoNormal, div.MsoNormal\n"
    "\t{margin:0cm; font-size:11.0pt; font-family:Calibri,sans-serif;}\n"
    "@page WordSection1 {size:595.3pt 841.9pt;}\n"
    "--></style>"
    '<div class="WordSection1">'
    '<p class="MsoNormal">обычный абзац</p>'
    '<p class="MsoNormal" style="font-family:Arial">автор задал свой</p>'
    "</div>"
)


async def test_a_font_set_by_a_css_class_survives_quoting(db, monkeypatch):
    """Word задаёт шрифт классами в <style>, а санитайзер вырезает и <style>, и class:
    цитата приходила системным шрифтом. Правила втягиваются в элементы до вырезания."""
    got = _capture(monkeypatch)
    async with db() as s:
        acc, orig = await _account_and_letter(s)
        orig.body_raw_html = OUTLOOK_HTML
        await s.commit()
        body = "Согласен." + _quote_block(orig, "reply")
        await compose.send(
            s, account_id=acc.id, to="boss@corp.ru", subject="Re: Планы", body=body,
            in_reply_to="<orig@corp.ru>", references=[],
        )
    html = got["html"]
    assert "Calibri" in html          # шрифт из класса доехал
    assert "<style" not in html       # а сам блок правил — нет
    assert "class=" not in html
    assert "@font-face" not in html and "WordSection1" not in html  # at-правила не втянуты


async def test_the_authors_own_inline_font_wins_over_the_class(db, monkeypatch):
    """Иначе втянутое правило однажды переопределит то, что автор написал явно."""
    got = _capture(monkeypatch)
    async with db() as s:
        acc, orig = await _account_and_letter(s)
        orig.body_raw_html = OUTLOOK_HTML
        await s.commit()
        body = "Согласен." + _quote_block(orig, "reply")
        await compose.send(
            s, account_id=acc.id, to="boss@corp.ru", subject="Re: Планы", body=body,
            in_reply_to="<orig@corp.ru>", references=[],
        )
    # У абзаца со своим style шрифт остаётся его, а не классовый.
    own = [p for p in got["html"].split("<p") if "автор задал свой" in p]
    assert own and "Arial" in own[0] and "Calibri" not in own[0]
