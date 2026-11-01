"""Provider presets + IMAP connection test (login + folder autodetection).

Presets fill host/port/folders and give the user provider-specific instructions for
creating an app-password (basic auth is dead on Gmail/Yandex — app-passwords required).
test_connection() does a real IMAP login and LISTs folders so the wizard can verify
before saving and auto-map Inbox/Sent.
"""

from __future__ import annotations

import asyncio
import imaplib
import re
from dataclasses import dataclass, field

PROVIDERS = {
    "gmail": {
        "label": "Gmail",
        "host": "imap.gmail.com",
        "port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "inbox": "INBOX",
        "sent": "[Gmail]/Sent Mail",
        "password_label": "Пароль приложения (App Password, 16 символов)",
        "deeplink": "https://myaccount.google.com/apppasswords",
        "deeplink_label": "Открыть App Passwords в Google",
        "steps": [
            "Включите двухэтапную аутентификацию в Google-аккаунте.",
            "Создайте «Пароль приложения» (App Password) — 16 символов.",
            "Вставьте его ниже. Обычный пароль Gmail для IMAP больше не работает.",
        ],
    },
    "yandex": {
        "label": "Yandex",
        "host": "imap.yandex.ru",
        "port": 993,
        "smtp_host": "smtp.yandex.ru",
        "smtp_port": 465,
        "inbox": "INBOX",
        "sent": "Sent",
        "password_label": "Пароль приложения",
        "deeplink": "https://id.yandex.ru/security/app-passwords",
        "deeplink_label": "Открыть пароли приложений в Yandex ID",
        "steps": [
            "В Почте: Настройки → Почтовые программы → включите доступ по IMAP "
            "и «Пароли приложений и OAuth-токены».",
            "В Yandex ID создайте пароль приложения типа «Почта».",
            "Вставьте его ниже. Обычный пароль Яндекса для IMAP не подойдёт.",
        ],
    },
    "imap": {
        "label": "Другой IMAP",
        "host": "",
        "port": 993,
        "smtp_host": "",
        "smtp_port": 465,
        "inbox": "INBOX",
        "sent": "Sent",
        "password_label": "Пароль",
        "deeplink": None,
        "deeplink_label": None,
        "steps": [
            "Узнайте IMAP-хост и порт у вашего провайдера (обычно порт 993, SSL).",
            "Если включена 2FA — создайте пароль приложения.",
        ],
    },
}

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) "?[^"]*"? (?P<name>.+)$')
_SENT_FLAG = b"\\Sent"
_SENT_NAMES = ("sent", "отправленны", "отправленые")


@dataclass
class TestResult:
    ok: bool
    error: str | None = None
    folders: list[str] = field(default_factory=list)
    suggested_inbox: str = "INBOX"
    suggested_sent: str | None = None


def _parse_folder(line: bytes) -> tuple[str, str] | None:
    """Return (flags, name) for an IMAP LIST line, or None."""
    m = _LIST_RE.match(line)
    if not m:
        return None
    flags = m.group("flags")
    name = m.group("name").strip()
    if name.startswith(b'"') and name.endswith(b'"'):
        name = name[1:-1]
    try:
        return flags.decode(), name.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def _autodetect(folders_with_flags: list[tuple[str, str]]) -> tuple[str, str | None]:
    inbox = "INBOX"
    sent = None
    for flags, name in folders_with_flags:
        if "\\Sent" in flags:
            sent = name
            break
    if sent is None:
        for _flags, name in folders_with_flags:
            low = name.lower()
            if any(s in low for s in _SENT_NAMES):
                sent = name
                break
    return inbox, sent


def _test_blocking(host: str, port: int, login: str, password: str) -> TestResult:
    # Cap the socket: this runs both from the wizard and from the sync worker's folder
    # self-heal, where a stalled server must not wedge the poll loop.
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=30)
    except OSError as exc:
        return TestResult(ok=False, error=f"Не удалось подключиться к {host}:{port} ({exc})")
    try:
        try:
            conn.login(login, password)
        except imaplib.IMAP4.error as exc:
            msg = str(exc)
            hint = ""
            if "AUTHENTICATIONFAILED" in msg.upper() or "Invalid" in msg or "Login" in msg:
                hint = " — проверьте логин и пароль приложения (обычный пароль не подойдёт)."
            return TestResult(ok=False, error=f"Аутентификация не удалась{hint}")
        typ, data = conn.list()
        parsed: list[tuple[str, str]] = []
        if typ == "OK":
            for line in data or []:
                if isinstance(line, (bytes, bytearray)):
                    p = _parse_folder(line)
                    if p:
                        parsed.append(p)
        inbox, sent = _autodetect(parsed)
        return TestResult(
            ok=True,
            folders=[n for _f, n in parsed],
            suggested_inbox=inbox,
            suggested_sent=sent,
        )
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


async def test_connection(host: str, port: int, login: str, password: str) -> TestResult:
    return await asyncio.to_thread(_test_blocking, host, port, login, password)
