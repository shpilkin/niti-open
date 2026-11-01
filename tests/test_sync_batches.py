"""Скачанные письма сохраняются пачками, а не одной транзакцией.

Первая синхронизация ящика приносит тысячи писем; пока они пишутся одной транзакцией,
в базу не может писать никто другой (docs/feature-storage-plan.md, этап 1).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

from email_reader.connectors import sync_worker
from email_reader.connectors.imap import FetchedMessage, FetchResult
from email_reader.models import Account, Email, SyncState
from tests.conftest import SPACE_ID, mk_space

ME = "me@b.ru"


def _raw(uid: int) -> bytes:
    return (
        f"From: Пётр <p{uid}@romashka.ru>\r\nTo: {ME}\r\nSubject: Смета {uid}\r\n"
        f"Message-ID: <{uid}@romashka.ru>\r\nDate: Wed, 17 Sep 2026 12:00:00 +0300\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\nПрошу подтвердить сроки.\r\n"
    ).encode()


@pytest.fixture
def synced(db, monkeypatch):
    """Синхронизация одной папки: сессии — тестовые, письма — из подменённого IMAP."""
    counted = {"transactions": 0}

    @asynccontextmanager
    async def scope():
        counted["transactions"] += 1
        async with db() as s:
            yield s
            await s.commit()

    async def fake_fetch(**_kw):
        # Сервер отдаёт вперемешку — пачки должны идти по возрастанию UID.
        return FetchResult(
            uidvalidity=7,
            messages=[FetchedMessage(uid=u, raw=_raw(u)) for u in (5, 1, 4, 2, 3)],
        )

    monkeypatch.setattr(sync_worker, "session_scope", scope)
    monkeypatch.setattr(sync_worker, "fetch_new", fake_fetch)
    monkeypatch.setattr(sync_worker, "STORE_BATCH", 2)
    return counted


async def _account(db) -> Account:
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="Работа", host="imap.b.ru", port=993,
                      login=ME, folders=["INBOX"], enabled=True)
        s.add(acc)
        await s.commit()
        return acc


async def _state(db) -> tuple[int, int]:
    async with db() as s:
        stored = (await s.execute(select(func.count(Email.id)))).scalar_one()
        st = (await s.execute(select(SyncState))).scalar_one()
        return stored, st.last_uid


async def test_letters_are_stored_in_batches(db, synced):
    acc = await _account(db)

    count = await sync_worker.sync_folder(acc, "pw", "INBOX", {ME}, False)

    stored, last_uid = await _state(db)
    assert (count, stored, last_uid) == (5, 5, 5)
    assert synced["transactions"] >= 4  # состояние + три пачки по два письма


async def test_a_failure_midway_keeps_the_letters_already_stored(db, synced, monkeypatch):
    acc = await _account(db)
    real_store = sync_worker.store_email

    async def store_until_uid_4(session, **kw):
        if kw["uid"] == 4:
            raise RuntimeError("связь оборвалась")
        return await real_store(session, **kw)

    monkeypatch.setattr(sync_worker, "store_email", store_until_uid_4)

    with pytest.raises(RuntimeError):
        await sync_worker.sync_folder(acc, "pw", "INBOX", {ME}, False)

    # Первая пачка (UID 1 и 2) на месте, и курсор стоит на ней: следующий заход
    # начнёт с 3, а не перекачает всё заново.
    stored, last_uid = await _state(db)
    assert (stored, last_uid) == (2, 2)
