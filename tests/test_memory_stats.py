"""Person stats: deterministic facts about a correspondent from the mailbox itself.

The numbers the assistant will later cite must be honest: CC does not make a message
unanswered, triage'd bulk mail does not count, another mailbox's mail is invisible when
scoped, and «unanswered» needs the silence to be older than STALE_DAYS.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from email_reader.enums import Direction
from email_reader.memory import stats as stats_svc
from email_reader.memory.stats import PersonStats, describe
from email_reader.models import Account, Email, Identity, Thread
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space

ME = "me@b.ru"
THEM = "petrov@romashka.ru"
NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)
D = timedelta(days=1)
H = timedelta(hours=1)


async def _account(s, name="acc", space=1):
    await mk_space(s, space)
    a = Account(
        space_id=uuid.UUID(int=space), name=name, host="imap.b.ru", port=993,
        login=name + "@b.ru",
    )
    s.add(a)
    await s.flush()
    return a


async def _thread(s, account):
    t = Thread(account_id=account.id)
    s.add(t)
    await s.flush()
    return t


def _addrs(*addrs):
    return [{"email": a, "name": None} for a in addrs]


def _mail(account, thread, *, direction, from_addr, to, at, cc=(), low=False):
    return Email(
        account_id=account.id, thread_id=thread.id, dedup_hash=uuid.uuid4().hex,
        direction=direction, from_addr=from_addr, to_addrs=_addrs(*to), cc_addrs=_addrs(*cc),
        sent_at=at, is_low_value=low,
    )


async def test_reply_rate_latency_and_unanswered(db):
    async with db() as s:
        acc = await _account(s)
        s.add(Identity(email=ME))
        t1 = await _thread(s, acc)  # they asked, I answered two hours later
        s.add(_mail(acc, t1, direction=Direction.INBOUND, from_addr=THEM, to=[ME],
                    at=NOW - 10 * D))
        s.add(_mail(acc, t1, direction=Direction.OUTBOUND, from_addr=ME, to=[THEM],
                    at=NOW - 10 * D + 2 * H))
        t2 = await _thread(s, acc)  # they asked five days ago, silence
        s.add(_mail(acc, t2, direction=Direction.INBOUND, from_addr=THEM, to=[ME], at=NOW - 5 * D))
        t3 = await _thread(s, acc)  # I'm only in CC: no obligation, so not unanswered
        s.add(_mail(acc, t3, direction=Direction.INBOUND, from_addr=THEM, to=["x@y.ru"],
                    cc=[ME], at=NOW - 6 * D))
        t4 = await _thread(s, acc)  # bulk mail from the same address is not correspondence
        s.add(_mail(acc, t4, direction=Direction.INBOUND, from_addr=THEM, to=[ME],
                    at=NOW - 1 * D, low=True))
        await s.commit()
        st = await stats_svc.person_stats(s, THEM, SPACE_ID, now=NOW)

    assert st.inbound == 3
    assert st.inbound_to_me == 2
    assert st.replied == 1
    assert st.unanswered == 1
    assert st.median_reply_hours == 2
    assert st.outbound == 1
    assert st.total_inbound == 3
    assert st.first_seen_at == NOW - 10 * D
    assert st.last_inbound_at == NOW - 5 * D


async def test_recent_silence_is_not_unanswered_yet(db):
    async with db() as s:
        acc = await _account(s)
        s.add(Identity(email=ME))
        t = await _thread(s, acc)
        s.add(_mail(acc, t, direction=Direction.INBOUND, from_addr=THEM, to=[ME], at=NOW - 1 * D))
        await s.commit()
        st = await stats_svc.person_stats(s, THEM, SPACE_ID, now=NOW)
    assert st.inbound_to_me == 1
    assert st.unanswered == 0


async def test_nag_sequence_counts_once_and_latency_uses_latest_message(db):
    async with db() as s:
        acc = await _account(s)
        s.add(Identity(email=ME))
        t = await _thread(s, acc)
        s.add(_mail(acc, t, direction=Direction.INBOUND, from_addr=THEM, to=[ME], at=NOW - 9 * D))
        s.add(_mail(acc, t, direction=Direction.INBOUND, from_addr=THEM, to=[ME], at=NOW - 8 * D))
        s.add(_mail(acc, t, direction=Direction.OUTBOUND, from_addr=ME, to=[THEM],
                    at=NOW - 8 * D + 3 * H))
        t2 = await _thread(s, acc)
        s.add(_mail(acc, t2, direction=Direction.INBOUND, from_addr=THEM, to=[ME], at=NOW - 7 * D))
        s.add(_mail(acc, t2, direction=Direction.INBOUND, from_addr=THEM, to=[ME], at=NOW - 4 * D))
        await s.commit()
        st = await stats_svc.person_stats(s, THEM, SPACE_ID, now=NOW)
    assert st.replied == 2          # both earlier messages got my answer
    assert st.median_reply_hours == 3  # measured from their latest message before mine
    assert st.unanswered == 1       # the second thread, once — not once per nag


async def test_scoped_to_space_not_mailbox(db):
    async with db() as s:
        work1 = await _account(s, "work1", space=1)
        work2 = await _account(s, "work2", space=1)
        home = await _account(s, "home", space=2)
        s.add(Identity(email=ME))
        for acc, days in ((work1, 2), (work2, 3), (home, 4)):
            t = await _thread(s, acc)
            s.add(_mail(acc, t, direction=Direction.INBOUND, from_addr=THEM, to=[ME],
                        at=NOW - days * D))
        await s.commit()
        work = await stats_svc.person_stats(s, THEM, SPACE_ID, now=NOW)
        personal = await stats_svc.person_stats(s, THEM, SPACE_ID_2, now=NOW)
        everything = await stats_svc.person_stats(s, THEM, None, now=NOW)
    assert work.inbound == 2       # both work mailboxes commingle inside the space
    assert personal.inbound == 1   # nothing crosses the space boundary
    assert everything.inbound == 3


async def test_address_is_matched_case_insensitively(db):
    async with db() as s:
        acc = await _account(s)
        s.add(Identity(email=ME))
        t = await _thread(s, acc)
        s.add(_mail(acc, t, direction=Direction.INBOUND, from_addr="Petrov@Romashka.RU",
                    to=[ME], at=NOW - 2 * D))
        await s.commit()
        st = await stats_svc.person_stats(s, " PETROV@romashka.ru ", SPACE_ID, now=NOW)
    assert st.inbound == 1


def test_describe_without_data():
    assert describe(PersonStats(email="x@y.ru")) == ["Переписки за 90 дней нет."]


def test_describe_reads_as_facts():
    st = PersonStats(
        email="x@y.ru", inbound=12, inbound_to_me=9, replied=8, unanswered=1,
        median_reply_hours=2.0, total_inbound=47,
        first_seen_at=datetime(2026, 1, 12, tzinfo=UTC),
    )
    lines = describe(st)
    assert lines[0] == "Пишет мне: 12 за 90 дней, лично мне — 9; всего 47 с 12.01.2026."
    assert lines[1] == "Я отвечаю: 8 из 9, обычно за 2 ч."
    assert lines[2] == "Ждут моего ответа: 1 (старше 3 дн.)."
