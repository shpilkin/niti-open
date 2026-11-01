"""Read-state write-back to IMAP (\\Seen).

Marking an email read/unread in Niti (including opening it, and completing a commitment)
must mirror the \\Seen flag to the server it came from — via a MarkSeen queue event the
worker handles, guarded by UIDVALIDITY so a folder renumber never flags the wrong message.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from email_reader import read_sync, views
from email_reader.connectors import imap
from email_reader.enums import Direction, EventType
from email_reader.models import (
    Account,
    ActionItem,
    Email,
    EmailOccurrence,
    Event,
    SyncState,
)
from tests.conftest import SPACE_ID, mk_space


async def _mk_email(s, *, account_id, read_at=None, direction=Direction.INBOUND):
    e = Email(
        account_id=account_id, dedup_hash=uuid.uuid4().hex, direction=direction,
        from_addr="a@b.ru", to_addrs=[], cc_addrs=[], sent_at=datetime.now(UTC),
        read_at=read_at,
    )
    s.add(e)
    await s.flush()
    return e


async def _mk_account(s, name="acc"):
    a = Account(space_id=SPACE_ID, name=name, host="imap.b.ru", port=993, login="me@b.ru")
    s.add(a)
    await s.flush()
    return a


def _capture(monkeypatch, result=None):
    calls: list[dict] = []

    async def fake_set_seen(**kw):
        calls.append(kw)
        return result

    monkeypatch.setattr(imap, "set_seen", fake_set_seen)
    monkeypatch.setattr(read_sync, "resolve_password", lambda acc: "pw")
    return calls


async def test_set_read_enqueues_mark_seen(db):
    async with db() as s:
        await mk_space(s)
        a = await _mk_account(s)
        e = await _mk_email(s, account_id=a.id)
        await s.commit()
        await views.set_read(s, e.id, True)
        await s.commit()

    async with db() as s:

        await mk_space(s)
        evs = (await s.execute(
            select(Event).where(Event.type == EventType.MARK_SEEN))).scalars().all()
        assert len(evs) == 1
        assert evs[0].payload["email_id"] == str(e.id)


async def test_mark_seen_sets_flag_with_uidvalidity(db, monkeypatch):
    calls = _capture(monkeypatch)
    async with db() as s:
        await mk_space(s)
        a = await _mk_account(s)
        e = await _mk_email(s, account_id=a.id, read_at=datetime.now(UTC))  # read
        s.add(EmailOccurrence(account_id=a.id, folder="INBOX", uid=42, email_id=e.id))
        s.add(SyncState(account_id=a.id, folder="INBOX", uidvalidity=7, last_uid=100))
        await s.commit()
        await read_sync.process_mark_seen(s, {"email_id": str(e.id)})

    assert len(calls) == 1
    assert calls[0]["seen"] is True
    assert calls[0]["uid"] == 42
    assert calls[0]["folder"] == "INBOX"
    assert calls[0]["expected_uidvalidity"] == 7  # guards against a renumbered folder


async def test_mark_seen_clears_flag_when_unread(db, monkeypatch):
    calls = _capture(monkeypatch)
    async with db() as s:
        await mk_space(s)
        a = await _mk_account(s)
        e = await _mk_email(s, account_id=a.id, read_at=None)  # unread
        s.add(EmailOccurrence(account_id=a.id, folder="INBOX", uid=1, email_id=e.id))
        await s.commit()
        await read_sync.process_mark_seen(s, {"email_id": str(e.id)})
    assert calls and calls[0]["seen"] is False  # symmetric: unread clears \Seen


async def test_mark_seen_uidvalidity_skip_does_not_raise(db, monkeypatch):
    _capture(monkeypatch, result=f"{imap.UIDVALIDITY_SKIP}: 9 != 7")
    async with db() as s:
        await mk_space(s)
        a = await _mk_account(s)
        e = await _mk_email(s, account_id=a.id, read_at=datetime.now(UTC))
        s.add(EmailOccurrence(account_id=a.id, folder="INBOX", uid=1, email_id=e.id))
        await s.commit()
        # A stale UID is permanent — must be swallowed, not retried forever.
        await read_sync.process_mark_seen(s, {"email_id": str(e.id)})


async def test_mark_seen_transient_error_raises_for_retry(db, monkeypatch):
    _capture(monkeypatch, result="connection reset")
    async with db() as s:
        await mk_space(s)
        a = await _mk_account(s)
        e = await _mk_email(s, account_id=a.id, read_at=datetime.now(UTC))
        s.add(EmailOccurrence(account_id=a.id, folder="INBOX", uid=1, email_id=e.id))
        await s.commit()
        with pytest.raises(RuntimeError):
            await read_sync.process_mark_seen(s, {"email_id": str(e.id)})


async def test_complete_marks_source_email_read(db):
    async with db() as s:
        await mk_space(s)
        a = await _mk_account(s)
        e = await _mk_email(s, account_id=a.id, read_at=None)
        ai = ActionItem(
            space_id=SPACE_ID, type="promise", title="сделать", source_message_id=e.id
        )
        s.add(ai)
        await s.flush()
        await views.complete(s, ai.id)
        await s.commit()

    async with db() as s:

        await mk_space(s)
        e2 = (await s.execute(select(Email).where(Email.id == e.id))).scalar_one()
        assert e2.read_at is not None  # marked read locally
        evs = (await s.execute(
            select(Event).where(Event.type == EventType.MARK_SEEN))).scalars().all()
        assert len(evs) == 1 and evs[0].payload["email_id"] == str(e.id)  # + queued for server


async def test_complete_syncs_even_if_already_read(db):
    """Regression: an email read locally BEFORE write-back existed is still unread on the
    server. Completing its commitment must push \\Seen regardless of local read state."""
    async with db() as s:
        await mk_space(s)
        a = await _mk_account(s)
        e = await _mk_email(s, account_id=a.id, read_at=datetime.now(UTC))  # already read
        ai = ActionItem(space_id=SPACE_ID, type="promise", title="x", source_message_id=e.id)
        s.add(ai)
        await s.flush()
        await views.complete(s, ai.id)
        await s.commit()

    async with db() as s:

        await mk_space(s)
        evs = (await s.execute(
            select(Event).where(Event.type == EventType.MARK_SEEN))).scalars().all()
        assert len(evs) == 1  # enqueued despite the email already being read locally


async def test_complete_marks_whole_thread_inbound(db):
    """The extracted source is often the FIRST message while the still-unread reply is a
    later one in the same thread. Completing must clear every inbound message of the
    conversation (and leave outbound alone)."""
    from email_reader.models import Thread

    async with db() as s:

        await mk_space(s)
        a = await _mk_account(s)
        t = Thread(account_id=a.id)
        s.add(t)
        await s.flush()
        first = await _mk_email(s, account_id=a.id, read_at=datetime.now(UTC))  # old, read
        first.thread_id = t.id
        mine = await _mk_email(s, account_id=a.id, direction=Direction.OUTBOUND)  # my reply
        mine.thread_id = t.id
        later = await _mk_email(s, account_id=a.id, read_at=None)  # the still-unread reply
        later.thread_id = t.id
        # Commitment points at the FIRST message, not the unread later one.
        ai = ActionItem(
            space_id=SPACE_ID,
            type="promise", title="reply", source_message_id=first.id, source_thread_id=t.id)
        s.add(ai)
        await s.flush()
        await views.complete(s, ai.id)
        await s.commit()

    async with db() as s:

        await mk_space(s)
        later2 = (await s.execute(select(Email).where(Email.id == later.id))).scalar_one()
        assert later2.read_at is not None  # the actually-unread reply got marked read
        seen_ids = {
            e.payload["email_id"]
            for e in (await s.execute(
                select(Event).where(Event.type == EventType.MARK_SEEN))).scalars().all()
        }
        assert str(first.id) in seen_ids and str(later.id) in seen_ids  # both inbound synced
        assert str(mine.id) not in seen_ids  # outbound left alone


def test_mailbox_quotes_folder_names():
    """IMAP mailbox names must be quoted; imaplib doesn't do it, so a spaced folder
    (Gmail's "[Gmail]/Sent Mail") otherwise fails STATUS with BAD 'Could not parse'."""
    assert imap._mailbox("INBOX") == '"INBOX"'
    assert imap._mailbox("[Gmail]/Sent Mail") == '"[Gmail]/Sent Mail"'
    # A literal quote/backslash in the name is escaped, staying a valid quoted string.
    assert imap._mailbox('od"d') == '"od\\"d"'


def test_remap_folders_relocates_localized_sent():
    """Gmail renames the sent folder per account language; a vanished non-INBOX folder is
    remapped to the server's SPECIAL-USE sent folder (kept in server modified-UTF-7 form)."""
    from email_reader.connectors import sync_worker
    r = sync_worker._remap_folders(
        ["INBOX", "[Gmail]/Sent Mail"],
        ["INBOX", "[Gmail]/&BB4EQgQ-", "[Gmail]/All"],
        "[Gmail]/&BB4EQgQ-",
    )
    assert r == ["INBOX", "[Gmail]/&BB4EQgQ-"]


def test_remap_folders_keeps_existing_and_inbox():
    from email_reader.connectors import sync_worker
    # A still-present folder is untouched; INBOX is kept even if LIST omits it.
    assert sync_worker._remap_folders(
        ["INBOX", "Sent"], ["Sent", "Drafts"], "Sent"
    ) == ["INBOX", "Sent"]


def test_remap_folders_keeps_unresolvable():
    from email_reader.connectors import sync_worker
    # Sent gone and no SPECIAL-USE sent on the server → keep it so the error stays visible
    # (don't silently stop syncing it); the list is unchanged, so heal is a no-op.
    assert sync_worker._remap_folders(["INBOX", "Sent"], ["INBOX"], None) == ["INBOX", "Sent"]
