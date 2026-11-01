"""The board orders by the sender history — from the cache, never computed on render."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from email_reader import views
from email_reader.enums import ActionStatus, ActionType, Direction
from email_reader.memory import stats as stats_svc
from email_reader.memory.stats import PersonStats, address_of, memory_score
from email_reader.models import Account, ActionItem, Email, Identity, PersonStat, Thread
from tests.conftest import SPACE_ID, mk_space

ME = "me@b.ru"
NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def test_address_of_reads_the_stored_counterparty():
    assert address_of("Пётр Петров (Petrov@Romashka.ru)") == "petrov@romashka.ru"
    assert address_of("petrov@romashka.ru") == "petrov@romashka.ru"
    assert address_of("Пётр Петров") is None
    assert address_of(None) is None


def test_memory_score_is_the_history_only_order():
    assert memory_score(None, ongoing=False) == 1.0          # unknown person: neutral 0.5 rate
    assert memory_score(None, ongoing=True) == 3.0
    fast = PersonStats(email="x", inbound_to_me=9, replied=8, median_reply_hours=2.0)
    slow = PersonStats(email="y", inbound_to_me=9, replied=1, median_reply_hours=80.0)
    assert memory_score(fast, ongoing=False) > memory_score(None, ongoing=False)
    assert memory_score(slow, ongoing=False) < memory_score(None, ongoing=False)


def test_order_key_deadline_still_beats_memory():
    a = ActionItem(space_id=SPACE_ID, type=ActionType.MY_COMMITMENT, title="a",
                   due_date=date(2026, 9, 6), created_at=NOW)
    b = ActionItem(space_id=SPACE_ID, type=ActionType.MY_COMMITMENT, title="b",
                   due_date=date(2026, 9, 7), created_at=NOW)
    assert (views.order_key(0, a, set(), None, memory=0.0)
            < views.order_key(0, b, set(), None, memory=5.0))
    # Same deadline: the stronger history first, whatever the age.
    older = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="o",
                       created_at=NOW - timedelta(days=3))
    newer = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="n",
                       created_at=NOW)
    assert (views.order_key(3, newer, set(), None, memory=4.0)
            < views.order_key(3, older, set(), None, memory=1.0))


async def _seed(s):
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name="w", host="h", login=ME, folders=[])
    s.add(acc)
    s.add(Identity(email=ME))
    await s.flush()
    threads = {}
    for who in ("a@x.ru", "b@x.ru"):
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()
        s.add(Email(
            account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
            direction=Direction.INBOUND, from_addr=who, to_addrs=[{"email": ME}],
            cc_addrs=[], sent_at=NOW - timedelta(hours=1),
        ))
        threads[who] = th
    items = {}
    for who, days_old in (("a@x.ru", 3), ("b@x.ru", 1)):
        ai = ActionItem(
            space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title=who,
            status=ActionStatus.OPEN, counterparty=f"Кто-то ({who})",
            source_thread_id=threads[who].id, created_at=NOW - timedelta(days=days_old),
        )
        s.add(ai)
        items[who] = ai
    await s.flush()
    return acc, threads, items


async def test_board_puts_the_person_i_usually_answer_first(db):
    async with db() as s:
        acc, threads, items = await _seed(s)
        await s.commit()
        # No history yet: age decides — the older card first.
        assert [r.item.title for r in (await views.board(s, "today", SPACE_ID)).rows] == [
            "a@x.ru", "b@x.ru",
        ]
        s.add(PersonStat(space_id=SPACE_ID, email="b@x.ru", inbound_to_me=6, replied=6,
                         median_reply_hours=3.0, computed_at=NOW))
        s.add(PersonStat(space_id=SPACE_ID, email="a@x.ru", inbound_to_me=6, replied=0,
                         computed_at=NOW))
        await s.commit()
        assert [r.item.title for r in (await views.board(s, "today", SPACE_ID)).rows] == [
            "b@x.ru", "a@x.ru",
        ]


async def test_board_prefers_a_thread_i_am_already_in(db):
    async with db() as s:
        acc, threads, items = await _seed(s)
        s.add(Email(
            account_id=acc.id, thread_id=threads["b@x.ru"].id, dedup_hash=uuid.uuid4().hex,
            direction=Direction.OUTBOUND, from_addr=ME, to_addrs=[{"email": "b@x.ru"}],
            cc_addrs=[], sent_at=NOW - timedelta(days=2),
        ))
        await s.commit()
        assert [r.item.title for r in (await views.board(s, "today", SPACE_ID)).rows] == [
            "b@x.ru", "a@x.ru",
        ]


async def test_refresh_stale_sweeps_live_counterparties_only(db):
    async with db() as s:
        acc, threads, items = await _seed(s)
        s.add(ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="gone",
                         status=ActionStatus.DISMISSED, counterparty="Никто (gone@x.ru)"))
        await s.commit()
        assert await stats_svc.refresh_stale(s, now=NOW) == 2
        assert await stats_svc.refresh_stale(s, now=NOW) == 0
        cached = await stats_svc.cached(s, SPACE_ID, {"a@x.ru", "b@x.ru", "gone@x.ru"})
        assert set(cached) == {"a@x.ru", "b@x.ru"}
        assert cached["a@x.ru"].inbound_to_me == 1
        # A day later the numbers are stale again — but only `limit` per sweep.
        later = NOW + timedelta(hours=25)
        assert await stats_svc.refresh_stale(s, limit=1, now=later) == 1
        assert await stats_svc.refresh_stale(s, limit=1, now=later) == 1
        assert await stats_svc.refresh_stale(s, limit=1, now=later) == 0


async def test_waiting_board_keeps_the_oldest_wait_first(db):
    """Where the other side owes me, the sender history says nothing — the backtest label
    was «I wrote back». Those rows keep their wait-age order."""
    async with db() as s:
        await mk_space(s)
        s.add(ActionItem(space_id=SPACE_ID, type=ActionType.WAITING_FOR_REPLY, title="old",
                         status=ActionStatus.OPEN, counterparty="Slow (slow@x.ru)",
                         created_at=NOW - timedelta(days=6)))
        s.add(ActionItem(space_id=SPACE_ID, type=ActionType.WAITING_FOR_REPLY, title="new",
                         status=ActionStatus.OPEN, counterparty="Fast (fast@x.ru)",
                         created_at=NOW - timedelta(days=1)))
        s.add(PersonStat(space_id=SPACE_ID, email="fast@x.ru", inbound_to_me=6, replied=6,
                         median_reply_hours=1.0, computed_at=NOW))
        s.add(PersonStat(space_id=SPACE_ID, email="slow@x.ru", inbound_to_me=6, replied=0,
                         computed_at=NOW))
        await s.commit()
        rows = (await views.board(s, "waiting", SPACE_ID)).rows
    assert [r.item.title for r in rows] == ["old", "new"]
