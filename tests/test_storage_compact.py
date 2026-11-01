"""Storage maintenance: the DB file's free space and VACUUM, plus the index churn that
made the file grow in the first place.

The old mail index was written by SQL triggers, and its update trigger fired on ANY
change to `emails` — including `read_at`, which the \\Seen absorb writes in bulk on every
sync. FTS5 keeps superseded documents until a merge, so it grew many times larger than the
text. That index is gone (b6d3f9a1c724); `search_index` replaces it and is written from
Python, only where a document actually changed. The test below is the same guarantee
against the new index.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from email_reader import maintenance
from email_reader.enums import Direction
from email_reader.models import Account, Email
from email_reader.search import index as ix
from email_reader.search import query as q
from tests.conftest import SPACE_ID, mk_space

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)


async def _seed(s, n: int = 40) -> list[Email]:
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@b.ru", folders=[])
    s.add(acc)
    await s.flush()
    mail = [
        Email(account_id=acc.id, dedup_hash=uuid.uuid4().hex, direction=Direction.INBOUND,
              from_addr="petrov@romashka.ru", to_addrs=[{"email": "me@b.ru"}], cc_addrs=[],
              subject=f"Смета {i}", body_text="Договор на поставку. " * 200,
              sent_at=NOW - timedelta(days=i))
        for i in range(n)
    ]
    s.add_all(mail)
    await s.commit()
    return mail


async def _index_rows(s) -> int:
    return (await s.execute(text("select count(*) from search_index_data"))).scalar_one()


async def test_marking_mail_read_does_not_rewrite_the_search_index(db):
    """The regression that cost 400 MB: reading mail must not touch the index, while a
    real text edit must still reach it."""
    async with db() as s:
        mail = await _seed(s)
        for one in mail:
            await ix.index_mail(s, one)
        await s.commit()
        base = await _index_rows(s)

        for one in mail:
            await s.execute(
                text("update emails set read_at = :t where id = :i"),
                {"t": NOW, "i": one.id.hex},
            )
        await s.commit()
        after_read = await _index_rows(s)

        mail[0].subject = "Новая смета"
        await ix.index_mail(s, mail[0])
        await s.commit()
        _, found = await q.search(s, "новая", space_id=None)

    assert after_read == base, "reading mail must not churn the index"
    assert [r.ref for r in found] == [mail[0].id.hex]


async def test_db_size_reports_the_file_and_its_free_pages(db, monkeypatch, tmp_path):
    path = tmp_path / "t.db"
    monkeypatch.setattr(maintenance, "db_path", lambda: path)
    async with db() as s:
        await _seed(s)
        size = await maintenance.db_size(s)
    assert size["bytes"] == path.stat().st_size > 0
    assert size["free_bytes"] >= 0


async def test_compact_rewrites_the_file_without_the_free_pages(db, monkeypatch, tmp_path):
    path = tmp_path / "t.db"
    monkeypatch.setattr(maintenance, "db_path", lambda: path)
    async with db() as sm:
        await _seed(sm, n=120)
    async with db() as s:
        await s.execute(text("delete from emails"))
        await s.commit()
        # WAL: the deletions live in the -wal file until a checkpoint, and the free pages
        # only appear in the main file afterwards — same as a long-running app.
        await s.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        before = await maintenance.db_size(s)
    assert before["free_bytes"] > 0, "deleting rows should leave free pages behind"

    monkeypatch.setattr("email_reader.db.get_engine", lambda: db.kw["bind"])
    result = await maintenance.compact()
    assert result["skipped"] is None
    assert result["freed"] > 0
    assert result["after"] == path.stat().st_size < result["before"]


async def test_compact_refuses_when_the_disk_is_too_small(db, monkeypatch, tmp_path):
    path = tmp_path / "t.db"
    monkeypatch.setattr(maintenance, "db_path", lambda: path)
    async with db() as s:
        await _seed(s)

    class _Usage:
        free = 1024

    monkeypatch.setattr(maintenance.shutil, "disk_usage", lambda _p: _Usage)
    result = await maintenance.compact()
    assert result["skipped"] == "no_disk_space"
    assert result["freed"] == 0
    assert result["need"] > result["free_disk"]
