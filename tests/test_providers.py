from __future__ import annotations

from email_reader.providers import PROVIDERS, _autodetect, _parse_folder


def test_presets_have_required_fields():
    for _key, p in PROVIDERS.items():
        assert p["inbox"] and "port" in p and "steps" in p
    assert PROVIDERS["gmail"]["host"] == "imap.gmail.com"
    assert PROVIDERS["yandex"]["sent"] == "Sent"


def test_parse_folder_simple():
    assert _parse_folder(b'(\\HasNoChildren) "/" "INBOX"') == ("\\HasNoChildren", "INBOX")


def test_parse_folder_gmail_sent_with_space():
    flags, name = _parse_folder(b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"')
    assert "\\Sent" in flags
    assert name == "[Gmail]/Sent Mail"


def test_parse_folder_unquoted_name():
    assert _parse_folder(b'(\\HasNoChildren) "/" Sent') == ("\\HasNoChildren", "Sent")


def test_autodetect_prefers_sent_flag():
    folders = [
        ("\\HasNoChildren", "INBOX"),
        ("\\HasNoChildren \\Sent", "[Gmail]/Sent Mail"),
        ("\\HasNoChildren", "Random"),
    ]
    inbox, sent = _autodetect(folders)
    assert inbox == "INBOX"
    assert sent == "[Gmail]/Sent Mail"


def test_autodetect_name_fallback_russian():
    folders = [("\\HasNoChildren", "INBOX"), ("\\HasNoChildren", "Отправленные")]
    _inbox, sent = _autodetect(folders)
    assert sent == "Отправленные"


def test_autodetect_none_when_no_sent():
    _inbox, sent = _autodetect([("\\HasNoChildren", "INBOX"), ("\\X", "Drafts")])
    assert sent is None
