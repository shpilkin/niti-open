from __future__ import annotations

import base64
from datetime import UTC, datetime
from email.utils import format_datetime

from email_reader.enums import Direction
from email_reader.normalization import (
    compute_dedup_hash,
    normalize_addr,
    normalize_subject,
    parse_raw_email,
    resolve_direction,
)

IDS = {"me@yandex.ru", "me.example@gmail.com"}


def test_direction_inbound():
    assert resolve_direction("ivan@acme.ru", ["me@yandex.ru"], IDS) == Direction.INBOUND


def test_direction_outbound():
    assert (
        resolve_direction("me@yandex.ru", ["ivan@acme.ru"], IDS) == Direction.OUTBOUND
    )


def test_direction_outbound_when_cc_self():
    # To external + Cc self -> still outbound (not 'self').
    assert (
        resolve_direction("me@yandex.ru", ["ivan@acme.ru", "me.example@gmail.com"], IDS)
        == Direction.OUTBOUND
    )


def test_direction_self_only_to_me():
    assert (
        resolve_direction("me@yandex.ru", ["me.example@gmail.com"], IDS)
        == Direction.SELF
    )


def test_direction_case_insensitive():
    assert resolve_direction("ME@Yandex.RU", ["Ivan@Acme.ru"], IDS) == Direction.OUTBOUND


def test_normalize_plus_tags_off_by_default():
    assert normalize_addr("jdoe+news@gmail.com") == "jdoe+news@gmail.com"


def test_normalize_plus_tags_on():
    assert (
        normalize_addr("J.D.O.E+news@gmail.com", normalize_plus_tags=True) == "jdoe@gmail.com"
    )


def test_normalize_subject_strips_prefixes_ru_en():
    assert normalize_subject("Re: Fwd: Ответ: Смета") == "смета"
    assert normalize_subject("RE[2]: Hello") == "hello"


def test_dedup_hash_stable_and_subject_insensitive_to_prefix():
    h1 = compute_dedup_hash("a@b.c", "Re: X", "2026-06-27T10:00:00", "body")
    h2 = compute_dedup_hash("a@b.c", "X", "2026-06-27T10:00:00", "body")
    assert h1 == h2


def _raw(from_, to, subject, body, message_id="<m1@acme.ru>", extra=""):
    date = format_datetime(datetime(2026, 6, 27, 10, 0, tzinfo=UTC))
    return (
        f"From: {from_}\r\n"
        f"To: {to}\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"{extra}"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{body}\r\n"
    ).encode()


def test_parse_basic():
    p = parse_raw_email(_raw("Ivan <ivan@acme.ru>", "me@yandex.ru", "Смета", "Привет"))
    assert p.from_.email == "ivan@acme.ru"
    assert p.from_.name == "Ivan"
    assert p.to[0].email == "me@yandex.ru"
    assert p.subject == "Смета"
    assert "Привет" in p.body_text
    assert p.rfc_message_id == "<m1@acme.ru>"


def test_parse_references_and_in_reply_to():
    extra = "In-Reply-To: <a@x>\r\nReferences: <a@x> <b@y>\r\n"
    p = parse_raw_email(_raw("a@x.com", "b@y.com", "Re: hi", "hello", extra=extra))
    assert p.in_reply_to == "<a@x>"
    assert p.references == ["<a@x>", "<b@y>"]


def test_parse_triage_headers_kept():
    extra = "List-Unsubscribe: <mailto:u@x>\r\nPrecedence: bulk\r\n"
    p = parse_raw_email(_raw("news@shop.com", "me@yandex.ru", "Sale", "buy", extra=extra))
    assert "list-unsubscribe" in p.headers
    assert p.headers["precedence"] == "bulk"


def test_parse_encoded_subject():
    # RFC2047 encoded-word (UTF-8 base64 "Привет")
    subj = "=?UTF-8?B?0J/RgNC40LLQtdGC?="
    p = parse_raw_email(_raw("a@x.com", "b@y.com", subj, "x"))
    assert p.subject == "Привет"


def _ew(text: str) -> str:
    import base64
    return "=?UTF-8?B?" + base64.b64encode(text.encode()).decode() + "?="


def test_parse_adjacent_encoded_words_drop_separating_space():
    # A name split mid-word across two adjacent, folded RFC2047 encoded-words: the
    # whitespace between them must be dropped (RFC 2047 §6.2), NOT kept as "Владимиров ич".
    from_ = f"{_ew('Иванов Алексей Владимиров')}\r\n {_ew('ич')} <ivanov@x.ru>"
    p = parse_raw_email(_raw(from_, "b@y.com", "t", "x"))
    assert p.from_.name == "Иванов Алексей Владимирович"
    assert p.from_.email == "ivanov@x.ru"


def _with_attached_letter(*, cte: str, own_text: str = "") -> bytes:
    """The letter as Yandex/Exchange send it: the real body plus a whole .eml attached."""
    inner = (
        b"From: someone@else.ru\r\nSubject: forwarded\r\nContent-Type: text/plain\r\n\r\n"
        b"chuzhoe pismo\r\n"
    )
    payload = base64.b64encode(inner).decode() if cte == "base64" else inner.decode()
    plain = (
        f"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{own_text}\r\n"
        if own_text
        else ""
    )
    return (
        "From: olga@x.ru\r\nTo: me@y.ru\r\nSubject: Смета\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        f"{plain}"
        "--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        "<div><p>Коллеги, добрый день!</p></div>\r\n"
        "--B\r\nContent-Type: message/rfc822\r\n"
        f"Content-Transfer-Encoding: {cte}\r\n"
        'Content-Disposition: attachment; filename="pismo.eml"\r\n\r\n'
        f"{payload}\r\n--B--\r\n"
    ).encode()


def test_attached_letter_does_not_become_the_body():
    """A base64 message/rfc822 part never parses into a message, so it surfaces with no
    headers at all — default text/plain, nothing to skip on. That is how a screen of
    base64 replaced the text of the letter."""
    p = parse_raw_email(_with_attached_letter(cte="base64"))
    assert not p.body_text
    assert "Коллеги, добрый день!" in p.body_html


def test_attached_letter_text_does_not_leak_into_the_body():
    p = parse_raw_email(_with_attached_letter(cte="7bit"))
    assert "chuzhoe pismo" not in (p.body_text or "")
    assert "Коллеги, добрый день!" in p.body_html


def test_own_text_wins_over_the_attached_letter():
    p = parse_raw_email(_with_attached_letter(cte="7bit", own_text="Мой текст"))
    assert p.body_text.strip() == "Мой текст"


def test_body_survives_a_header_folded_into_the_encoding():
    """A real letter arrived with «Content-Transfer-Encoding: base64 риложения в аккаунте
    …» — the subject bled into the header. The value no longer matched «base64», nothing
    was decoded, and the whole body was stored as a screen of base64."""
    body = base64.b64encode("<p>Вы создали пароль</p>".encode()).decode()
    raw = (
        "From: no-reply@corpmail.ru\r\nTo: me@y.ru\r\nSubject: Пароль\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64 риложения в аккаунте jdoe\r\n\r\n"
        f"{body}\r\n"
    ).encode()
    assert "Вы создали пароль" in parse_raw_email(raw).body_html


def test_a_normal_encoding_header_is_left_alone():
    body = base64.b64encode("<p>обычное письмо</p>".encode()).decode()
    raw = (
        "From: a@b.c\r\nTo: me@y.ru\r\nSubject: S\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{body}\r\n"
    ).encode()
    assert "обычное письмо" in parse_raw_email(raw).body_html
