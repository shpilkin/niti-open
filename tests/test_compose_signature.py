from __future__ import annotations

from email_reader import accounts as accounts_svc
from email_reader import compose
from email_reader import signatures as sigs
from email_reader.compose import (
    _FWD_MARKER,
    _extract_inline_sig,
    _reply_all_recipients,
    _split_quote,
)


def test_split_quote_forward():
    body = f"мой текст\n\n{_FWD_MARKER}\nОт: X\n\nоригинал\n"
    before, quote = _split_quote(body)
    assert before == "мой текст"
    assert quote.startswith(_FWD_MARKER)


def test_split_quote_reply():
    body = "мой ответ\n\n02.06.2026 10:00, Босс писал(а):\n> оригинал\n"
    before, quote = _split_quote(body)
    assert before == "мой ответ"
    assert "писал(а):" in quote


def test_split_quote_none():
    assert _split_quote("просто текст") == ("просто текст", "")


def test_extract_inline_sig():
    main, sig = _extract_inline_sig("текст\n\n-- \nГлеб\nPM")
    assert main == "текст"
    assert sig == "Глеб\nPM"
    assert _extract_inline_sig("текст без подписи") == ("текст без подписи", None)


def test_reply_all_recipients_excludes_me_and_dedups():
    to, cc = _reply_all_recipients(
        "boss@x.ru",
        [{"email": "me@ya.ru"}, {"email": "yana@x.ru"}, {"email": "Boss@X.ru"}],
        [{"email": "kirill@x.ru"}, {"email": "me@ya.ru"}, {"email": "yana@x.ru"}],
        identities={"me@ya.ru"},
    )
    assert to == "boss@x.ru, yana@x.ru"  # me excluded, boss deduped case-insensitively
    assert cc == "kirill@x.ru"  # me and already-in-To excluded


def _sender_capture(monkeypatch):
    captured = {}

    async def fake_send_mail(account, password, *, to, subject, body,
                             in_reply_to=None, references=None, attachments=None,
                             html_body=None, cc=""):
        captured["text"] = body
        captured["html"] = html_body
        captured["cc"] = cc
        from email_reader.mailer import build_message
        return build_message(account.login, to, subject, body, html_body=html_body, cc=cc)

    monkeypatch.setattr(compose, "send_mail", fake_send_mail)
    return captured


async def _acc_with_sig(s):
    acc = await accounts_svc.create_account(
        s, name="Y", provider="yandex", host="imap.yandex.ru", port=993,
        login="me@y.ru", password="secret", folders=["INBOX", "Sent"],
    )
    await sigs.set_signature(s, acc.id, '<b style="color:red">Глеб</b>')
    return acc


async def test_untouched_inline_sig_becomes_rich_before_quote(db, monkeypatch):
    """The form pre-fills '-- \\nГлеб' inline; send swaps it for the rich HTML sig,
    still before the forwarded block."""
    captured = _sender_capture(monkeypatch)
    async with db() as s:
        acc = await _acc_with_sig(s)
        body = f"Смотри вложение\n\n-- \nГлеб\n\n{_FWD_MARKER}\nОт: Босс\n\nтело\n"
        result = await compose.send(
            s, account_id=acc.id, to="x@x.com", subject="Fwd: Отчёт", body=body,
            in_reply_to=None, references=[],
        )
    assert result.error is None
    text, html = captured["text"], captured["html"]
    assert text.index("Глеб") < text.index(_FWD_MARKER)
    assert text.index("-- ") < text.index(_FWD_MARKER)
    assert html.index("Глеб") < html.index(_FWD_MARKER)
    assert "color:red" in html  # untouched -> styled signature survives


async def test_edited_inline_sig_wins_verbatim(db, monkeypatch):
    captured = _sender_capture(monkeypatch)
    async with db() as s:
        acc = await _acc_with_sig(s)
        body = "Ответ\n\n-- \nПросто Глеб\n\n02.06.2026 10:00, Босс писал(а):\n> оригинал\n"
        result = await compose.send(
            s, account_id=acc.id, to="x@x.com", subject="Re: X", body=body,
            in_reply_to=None, references=[],
        )
    assert result.error is None
    assert "Просто Глеб" in captured["text"]  # user's edit wins
    assert "color:red" not in captured["html"]  # rich version NOT substituted


async def test_deleted_inline_sig_is_not_readded(db, monkeypatch):
    captured = _sender_capture(monkeypatch)
    async with db() as s:
        acc = await _acc_with_sig(s)
        body = "Ответ без подписи\n\n02.06.2026 10:00, Босс писал(а):\n> оригинал\n"
        result = await compose.send(
            s, account_id=acc.id, to="x@x.com", subject="Re: X", body=body,
            in_reply_to=None, references=[],
        )
    assert result.error is None
    assert "Глеб" not in captured["text"]  # deletion respected
    assert "-- " not in captured["text"]


async def test_cc_passes_through_to_mailer(db, monkeypatch):
    captured = _sender_capture(monkeypatch)
    async with db() as s:
        acc = await _acc_with_sig(s)
        result = await compose.send(
            s, account_id=acc.id, to="x@x.com", subject="X", body="привет",
            in_reply_to=None, references=[], cc="kirill@x.ru, yana@x.ru",
        )
    assert result.error is None
    assert captured["cc"] == "kirill@x.ru, yana@x.ru"


async def test_a_missing_account_is_an_error_not_a_crash(db):
    """Раньше отсюда возвращалась голая строка, а вызывающий уже спрашивает .error —
    отправка на удалённый ящик падала бы AttributeError вместо сообщения."""
    import uuid as _uuid

    from email_reader import compose

    async with db() as s:
        result = await compose.send(
            s, account_id=_uuid.uuid4(), to="x@x.com", subject="X", body="привет",
            in_reply_to=None, references=[],
        )
    assert result.error == "Аккаунт не найден"


def test_the_letter_takes_the_font_of_its_signature():
    """Текст уходил системным шрифтом, а подпись — корпоративным, и письмо выглядело
    склеенным из двух. Шрифт письма берётся из подписи, если она его задаёт."""
    from email_reader import compose

    sig = ('<div style="font-family:&quot;Calibri&quot;,sans-serif;font-size:11pt">'
           'Глеб Миронов</div>')
    style = compose._body_style(sig)

    assert "font-family:Calibri,sans-serif" in style.replace(", ", ",")
    assert "font-size:11pt" in style
    # Без подписи (или без шрифта в ней) остаётся прежний системный стиль.
    assert compose._body_style(None) == compose._HTML_DIV
    assert compose._body_style("<div>без стилей</div>") == compose._HTML_DIV


async def test_the_letter_does_not_depend_on_how_its_html_is_wrapped(db, monkeypatch):
    """С white-space:pre-wrap перенос строки в исходнике HTML рисуется как ещё одна
    пустая строка. Почтовая программа, собирая письмо, может перенести строку после
    каждого <br> — и письмо уходило с двойными отступами между абзацами."""
    captured = _sender_capture(monkeypatch)
    async with db() as s:
        acc = await _acc_with_sig(s)
        result = await compose.send(
            s, account_id=acc.id, to="x@x.com", subject="Тема",
            body="Первый абзац\n\nВторой абзац", in_reply_to=None, references=[],
        )
    assert result.error is None
    html = captured["html"]
    assert "pre-wrap" not in html
    assert "Первый абзац<br><br>Второй абзац" in html


async def test_runs_of_spaces_survive_without_pre_wrap(db, monkeypatch):
    """Отступы, которыми человек выравнивал строки, HTML схлопнул бы в один пробел."""
    captured = _sender_capture(monkeypatch)
    async with db() as s:
        acc = await _acc_with_sig(s)
        result = await compose.send(
            s, account_id=acc.id, to="x@x.com", subject="Тема",
            body="Итого:    100", in_reply_to=None, references=[],
        )
    assert result.error is None
    assert "Итого:&nbsp;&nbsp;&nbsp; 100" in captured["html"]
    assert "Итого:    100" in captured["text"]  # текстовая версия — как набрано
