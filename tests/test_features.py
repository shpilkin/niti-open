"""Unit tests for the search query builder, attachment detection and forward build."""

from __future__ import annotations

from email_reader.mailer import build_message
from email_reader.normalization import parse_raw_email
from email_reader.search.query import match_expr, parse


def test_the_match_expression_quotes_every_word_and_pairs_it_with_its_stem():
    """Was `views._fts_query`, which built a prefix-AND over the mail-only index. The
    guarantee that carried over: user text can never inject FTS5 syntax, because every
    token is quoted, and nothing but tokens reaches the expression."""
    expr = match_expr(parse("договор аренды"))
    assert '"договор" AND "аренды"' in expr                    # exact forms, quoted
    assert '"договор" AND ("аренды" OR "аренд")' in expr       # and stems beside them
    assert "{title body people}" in expr and "{title_stem body_stem}" in expr

    # Operator characters are separators, not syntax: `OR` survives only as a word.
    hostile = match_expr(parse('a OR b" *'))
    assert '"a" AND "or" AND "b"' in hostile
    assert '"\\"' not in hostile and " * " not in hostile

    assert match_expr(parse("   ")) == ""


def _raw(extra_parts: str = "") -> bytes:
    return (
        "From: Bob <bob@x.com>\r\nTo: me@y.ru\r\nSubject: t\r\n"
        "Message-ID: <m@x>\r\nDate: Mon, 30 Jun 2026 10:00:00 +0000\r\n"
        + extra_parts
    ).encode()


def test_parse_detects_no_attachment():
    raw = _raw("Content-Type: text/plain; charset=utf-8\r\n\r\nhello\r\n")
    assert parse_raw_email(raw).has_attachments is False


def test_parse_detects_attachment():
    raw = _raw(
        'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        "--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n"
        "--b\r\nContent-Type: application/pdf\r\n"
        'Content-Disposition: attachment; filename="doc.pdf"\r\n\r\n%PDF-1\r\n'
        "--b--\r\n"
    )
    assert parse_raw_email(raw).has_attachments is True


def test_build_message_with_attachments():
    msg = build_message(
        "me@y.ru", "x@x.com", "Fwd: t", "see attached",
        attachments=[{"name": "a.pdf", "mime": "application/pdf", "bytes": b"%PDF-1"}],
    )
    assert msg.is_multipart()
    assert any(p.get_filename() == "a.pdf" for p in msg.walk())
