"""Открытие письма не должно зависеть от того, удалось ли что-то записать."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import Request

import email_reader.api.app  # noqa: F401 — регистрирует фильтры шаблонов
from email_reader import db as db_mod
from email_reader import views
from email_reader.api.routers import mail as mail_router
from email_reader.enums import Direction
from email_reader.models import Account, Email, Thread
from tests.conftest import SPACE_ID, mk_space


def _request() -> Request:
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "path": "/email/x",
        "raw_path": b"/email/x", "query_string": b"", "headers": [], "client": ("t", 1),
        "server": ("t", 80), "scheme": "http", "root_path": "", "app": None,
    }
    return Request(scope)


@pytest.fixture
def wired(db, monkeypatch):
    """Маршруты ходят в базу через session_scope, а не через фикстуру — подменяем её."""
    monkeypatch.setattr(db_mod, "_sessionmaker", db)
    return db


async def _letter(s) -> Email:
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@ya.ru", folders=[])
    s.add(acc)
    await s.flush()
    th = Thread(account_id=acc.id)
    s.add(th)
    await s.flush()
    e = Email(account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
              direction=Direction.INBOUND, from_addr="boss@corp.ru", to_addrs=[], cc_addrs=[],
              subject="Смета", body_text="тело", sent_at=datetime.now(UTC))
    s.add(e)
    await s.commit()
    return e


async def test_a_letter_opens_even_when_marking_it_read_fails(wired, monkeypatch):
    """Пометка «прочитано» шла внутри той же транзакции, что и чтение беседы. В WAL
    повышение читающей транзакции до пишущей даёт «database is locked» сразу, минуя
    busy_timeout, — и письмо не открывалось, хотя строка в списке уже подсветилась."""
    async with wired() as s:
        email = await _letter(s)

    async def boom(*a, **kw):
        raise OSError("database is locked")

    monkeypatch.setattr(views, "set_read", boom)
    resp = await mail_router.email_pane(_request(), email.id, back="")
    body = resp.body.decode()
    assert "Смета" in body          # письмо открылось
    assert "не найдено" not in body


async def test_opening_a_letter_still_marks_it_read(wired):
    async with wired() as s:
        email = await _letter(s)
    await mail_router.email_pane(_request(), email.id, back="")
    async with wired() as s:
        assert (await views.get_email(s, email.id)).read_at is not None
