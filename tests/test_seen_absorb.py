"""Read elsewhere: the sync asks the server which locally-unread messages are already
\\Seen and marks them read — without a MarkSeen event echoing the flag back."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from email_reader.connectors import imap, sync_worker
from email_reader.enums import Direction
from email_reader.models import Account, Email, EmailOccurrence, Event
from tests.conftest import SPACE_ID, mk_space

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


class FakeConn:
    """Just enough of imaplib for _fetch_blocking: no new mail, a fixed \\Seen set."""

    def __init__(self, seen: set[int]):
        self.seen = seen
        self.searches: list[tuple] = []

    def login(self, user, password):
        return "OK", []

    def status(self, mailbox, what):
        return "OK", [b'"INBOX" (UIDVALIDITY 7)']

    def select(self, mailbox, readonly=True):
        return "OK", [b"3"]

    def uid(self, cmd, *args):
        if cmd == "search":
            self.searches.append(args)
            if "SEEN" in args:
                asked = {int(u) for u in args[args.index("UID") + 1].split(",")}
                hits = sorted(asked & self.seen)
                return "OK", [" ".join(str(u) for u in hits).encode()]
            return "OK", [b""]
        return "OK", []

    def logout(self):
        return "BYE", []


def test_search_seen_asks_in_chunks_and_returns_the_intersection():
    conn = FakeConn(seen={5, 250, 450, 999})
    got = imap._search_seen(conn, list(range(1, 451)))
    assert got == {5, 250, 450}
    assert len(conn.searches) == 3  # 450 UIDs / 200 per command


def test_fetch_blocking_reports_seen_on_the_same_connection(monkeypatch):
    conn = FakeConn(seen={3, 4, 99})
    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", lambda host, port, timeout=None: conn)
    r = imap._fetch_blocking("h", 993, "l", "p", "INBOX", last_uid=10, unread_uids=[2, 3, 4])
    assert r.uidvalidity == 7
    assert r.messages == []
    assert r.seen_uids == {3, 4}


def test_fetch_blocking_skips_the_seen_search_when_nothing_is_unread(monkeypatch):
    conn = FakeConn(seen={3})
    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", lambda host, port, timeout=None: conn)
    r = imap._fetch_blocking("h", 993, "l", "p", "INBOX", last_uid=10, unread_uids=[])
    assert r.seen_uids == set()
    assert not [a for a in conn.searches if "SEEN" in a]


async def _account(s):
    await mk_space(s)
    a = Account(space_id=SPACE_ID, name="acc", host="imap.b.ru", port=993, login="me@b.ru")
    s.add(a)
    await s.flush()
    return a


async def _email(s, account, *, read_at=None, sent_at=NOW):
    e = Email(
        account_id=account.id, dedup_hash=uuid.uuid4().hex, direction=Direction.INBOUND,
        from_addr="a@b.ru", to_addrs=[], cc_addrs=[], sent_at=sent_at, read_at=read_at,
    )
    s.add(e)
    await s.flush()
    return e


async def test_absorb_marks_only_server_seen_unread_in_that_folder(db):
    async with db() as s:
        a = await _account(s)
        e1 = await _email(s, a)                 # unread, server still unseen
        e2 = await _email(s, a)                 # unread, read in another client
        e3 = await _email(s, a, read_at=NOW)    # already read locally
        e4 = await _email(s, a)                 # unread, but lives in another folder
        old = await _email(s, a, sent_at=datetime.now(UTC) - timedelta(days=400))
        s.add(EmailOccurrence(account_id=a.id, folder="INBOX", uid=1, email_id=e1.id))
        s.add(EmailOccurrence(account_id=a.id, folder="INBOX", uid=2, email_id=e2.id))
        s.add(EmailOccurrence(account_id=a.id, folder="INBOX", uid=3, email_id=e3.id))
        s.add(EmailOccurrence(account_id=a.id, folder="Sent", uid=2, email_id=e4.id))
        s.add(EmailOccurrence(account_id=a.id, folder="INBOX", uid=5, email_id=old.id))
        await s.commit()

        assert await sync_worker._unread_uids(s, a.id, "INBOX") == [1, 2, 5]
        # Outside the sync window: never asked about, so never a SEARCH round trip.
        assert await sync_worker._unread_uids(s, a.id, "INBOX", window_days=90) == [1, 2]
        n = await sync_worker.absorb_server_seen(s, a.id, "INBOX", {2, 3}, now=NOW)
        await s.commit()

    assert n == 1
    async with db() as s:
        by_id = {e.id: e for e in (await s.execute(select(Email))).scalars().all()}
        assert by_id[e1.id].read_at is None
        assert by_id[e2.id].read_at == NOW
        assert by_id[e3.id].read_at == NOW
        assert by_id[e4.id].read_at is None
        # The server already has the flag: no MarkSeen round trip is queued.
        assert (await s.execute(select(Event))).scalars().all() == []


async def test_absorb_with_nothing_seen_is_a_noop(db):
    async with db() as s:
        a = await _account(s)
        assert await sync_worker.absorb_server_seen(s, a.id, "INBOX", set()) == 0
