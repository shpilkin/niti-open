from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from email_reader import views
from email_reader.models import Email


async def _seed_inbound(s, n: int):
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    for i in range(n):
        s.add(Email(
            direction="inbound", from_addr=f"s{i}@ex.com", from_name=f"S{i}",
            to_addrs=[], cc_addrs=[], subject=f"msg {i}", body_text="body",
            sent_at=base + timedelta(minutes=i), dedup_hash=str(uuid.uuid4()),
        ))
    await s.flush()


async def test_list_mail_paginates_and_counts(db):
    async with db() as s:
        await _seed_inbound(s, 130)

        total = await views.count_mail(s, "inbox")
        assert total == 130

        page0 = await views.list_mail(s, "inbox", limit=50, offset=0)
        page1 = await views.list_mail(s, "inbox", limit=50, offset=50)
        page2 = await views.list_mail(s, "inbox", limit=50, offset=100)
        assert len(page0) == 50
        assert len(page1) == 50
        assert len(page2) == 30  # remainder

        # Pages are disjoint (ordered by sent_at desc, stable slicing).
        ids = {e.id for e in page0} | {e.id for e in page1} | {e.id for e in page2}
        assert len(ids) == 130


async def test_count_mail_empty_is_zero(db):
    async with db() as s:
        assert await views.count_mail(s, "inbox") == 0
        # Explicit empty label filter -> no rows, no error.
        assert await views.list_mail(s, "inbox", thread_ids=[]) == []
        assert await views.count_mail(s, "inbox", thread_ids=[]) == 0
