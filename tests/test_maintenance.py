"""Local-prune maintenance: deletes old local data, never touches the server, and
leaves no dangling foreign keys. Uses an isolated temp SQLite DB + data dir."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from email_reader import maintenance
from email_reader.models import (
    Account,
    ActionItem,
    Base,
    Email,
    EmailOccurrence,
    Thread,
)
from tests.conftest import SPACE_ID, mk_space


@pytest.fixture
async def sessionmaker(tmp_path):
    os.environ["EMAIL_READER_DATA_DIR"] = str(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")

    async def _fk(conn):
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


async def _seed(sm, tmp_path):
    now = datetime.now(UTC)
    old, recent = now - timedelta(days=200), now - timedelta(days=10)
    async with sm() as s:
        await mk_space(s)
        acc = Account(
            space_id=SPACE_ID, name="A", provider="yandex", host="h", port=993, login="me@y.ru",
                      password_enc="x", folders=["INBOX"], enabled=True)
        s.add(acc)
        await s.flush()
        t_old = Thread(last_at=old)
        s.add(t_old)
        await s.flush()
        e_old = Email(direction="inbound", from_addr="x@e.com", to_addrs=[], cc_addrs=[],
                      subject="old", body_text="b", sent_at=old, thread_id=t_old.id,
                      dedup_hash="h1")
        s.add(e_old)
        await s.flush()
        s.add(EmailOccurrence(email_id=e_old.id, account_id=acc.id, folder="INBOX", uid=1))
        s.add(ActionItem(
            space_id=SPACE_ID, type="MY_COMMITMENT", title="old", source_thread_id=t_old.id,
                         source_message_id=e_old.id, status="open"))
        t_new = Thread(last_at=recent)
        s.add(t_new)
        await s.flush()
        s.add(Email(direction="inbound", from_addr="y@e.com", to_addrs=[], cc_addrs=[],
                    subject="new", body_text="b", sent_at=recent, thread_id=t_new.id,
                    dedup_hash="h2"))
        cache = tmp_path / "attachments" / str(e_old.id)
        cache.mkdir(parents=True)
        (cache / "f.pdf").write_bytes(b"x")
        await s.commit()
        return e_old.id


async def test_prune_removes_old_keeps_recent(sessionmaker, tmp_path):
    old_eid = await _seed(sessionmaker, tmp_path)
    before = maintenance.cutoff_for(3, "months")

    async with sessionmaker() as s:
        prev = await maintenance.preview_prune(s, before)
    assert (prev["emails"], prev["threads"], prev["action_items"]) == (1, 1, 1)

    async with sessionmaker() as s:
        res = await maintenance.prune_local_before(s, before)
        await s.commit()
    assert res == {"emails": 1, "threads": 1, "action_items": 1}

    async with sessionmaker() as s:
        assert (await s.execute(select(func.count(Email.id)))).scalar_one() == 1  # recent kept
        assert (await s.execute(select(func.count(Thread.id)))).scalar_one() == 1
        assert (await s.execute(select(func.count(ActionItem.id)))).scalar_one() == 0
        fk = (await s.execute(text("PRAGMA foreign_key_check"))).fetchall()
    assert fk == []  # no dangling references
    assert not (tmp_path / "attachments" / str(old_eid)).exists()  # cache cleared


async def test_prune_noop_when_nothing_old(sessionmaker, tmp_path):
    await _seed(sessionmaker, tmp_path)
    before = maintenance.cutoff_for(10, "months")  # older than everything -> nothing matches
    async with sessionmaker() as s:
        res = await maintenance.prune_local_before(s, before)
        await s.commit()
    assert res == {"emails": 0, "threads": 0, "action_items": 0}
