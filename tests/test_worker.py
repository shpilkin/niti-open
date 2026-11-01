"""Worker resilience.

Root cause of the "database is locked" crash when saving a mailbox's model: the worker
claimed an event (PROCESSING) and held that write lock across the handler — which may
block on a slow LLM call for seconds — so concurrent writers (the web UI) timed out. The
fix commits the claim in its OWN short transaction, then runs the handler separately.

Also: a handler exception must be isolated — process_one catches it and reschedules the
event, never propagating (which, via asyncio.gather, would crash the whole backend).

We point worker.session_scope at the test DB so this exercises the real process_one.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from email_reader.enums import EventStatus, EventType
from email_reader.models import Event
from email_reader.pipeline import worker
from email_reader.queue import enqueue


@pytest.fixture
def worker_db(db, monkeypatch):
    """Make worker.process_one use the isolated test DB (a sessionmaker)."""
    @asynccontextmanager
    async def scope():
        async with db() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    monkeypatch.setattr(worker, "session_scope", scope)
    return db


async def test_claim_committed_before_handler(worker_db, monkeypatch):
    async with worker_db() as s:
        await enqueue(s, EventType.EMAIL_STORED, {"x": 1})
        await s.commit()

    seen: dict = {}

    async def fake_handler(session, payload):
        # A SEPARATE session must already see the claim committed as PROCESSING — proof
        # the worker isn't holding the write lock across the (slow) handler.
        async with worker_db() as s2:
            ev = (await s2.execute(select(Event))).scalars().first()
            seen["status"] = ev.status

    monkeypatch.setattr(worker, "_handlers", lambda: {EventType.EMAIL_STORED: fake_handler})
    assert await worker.process_one() is True
    assert seen["status"] == EventStatus.PROCESSING

    async with worker_db() as s:
        ev = (await s.execute(select(Event))).scalars().first()
        assert ev.status == EventStatus.DONE


async def test_handler_failure_is_isolated(worker_db, monkeypatch):
    async with worker_db() as s:
        await enqueue(s, EventType.EMAIL_STORED, {"x": 1})
        await s.commit()

    async def boom(session, payload):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(worker, "_handlers", lambda: {EventType.EMAIL_STORED: boom})
    # Must NOT raise — failure is caught and the event rescheduled.
    assert await worker.process_one() is True

    async with worker_db() as s:
        ev = (await s.execute(select(Event))).scalars().first()
        assert ev.status == EventStatus.PENDING
        assert ev.attempts == 1
        assert "kaboom" in ev.payload.get("last_error", "")


async def test_process_one_no_events_returns_false(worker_db):
    assert await worker.process_one() is False


async def test_model_unavailable_parks_as_stalled(worker_db, monkeypatch):
    """A model/infra outage must NOT burn the retry budget or hard-fail — it parks the
    event as STALLED (attempts untouched) so it can resume when the model returns."""
    import zlib

    async with worker_db() as s:
        await enqueue(s, EventType.THREAD_READY, {"thread_id": "t1"})
        await s.commit()

    async def unavailable(session, payload):
        raise zlib.error("Error -3 while decompressing data: incorrect header check")

    monkeypatch.setattr(worker, "_handlers", lambda: {EventType.THREAD_READY: unavailable})
    assert await worker.process_one() is True

    async with worker_db() as s:
        ev = (await s.execute(select(Event))).scalars().first()
        assert ev.status == EventStatus.STALLED
        assert ev.attempts == 0  # budget preserved across the outage


async def test_reclassify_and_revive_stalled(worker_db):
    from email_reader.queue import reclassify_failed_as_stalled, revive_stalled

    async with worker_db() as s:
        # A model-outage failure and a genuine parse failure, both already FAILED.
        s.add(Event(
            type=EventType.THREAD_READY, status=EventStatus.FAILED, attempts=5,
            payload={"last_error": "PermissionDeniedError('<html>403 Forbidden')"},
        ))
        s.add(Event(
            type=EventType.THREAD_READY, status=EventStatus.FAILED, attempts=5,
            payload={"last_error": "Expecting value: line 1 column 1"},
        ))
        await s.commit()

    async with worker_db() as s:
        assert await reclassify_failed_as_stalled(s) == 1  # only the model-outage one
        await s.commit()

    async with worker_db() as s:
        by_status = {}
        for ev in (await s.execute(select(Event))).scalars().all():
            by_status[ev.status] = by_status.get(ev.status, 0) + 1
        assert by_status.get(EventStatus.STALLED) == 1
        assert by_status.get(EventStatus.FAILED) == 1  # genuine failure untouched

    async with worker_db() as s:
        assert await revive_stalled(s) == 1
        await s.commit()

    async with worker_db() as s:
        ev = (await s.execute(
            select(Event).where(Event.status == EventStatus.PENDING)
        )).scalars().first()
        assert ev is not None and "403" in ev.payload["last_error"]


async def test_stalled_ceiling_fails_only_the_expired(worker_db):
    """A permanently-dead model must not park events forever: past the ceiling they
    become real FAILED, while freshly-parked events keep waiting."""
    from datetime import UTC, datetime, timedelta

    from email_reader.queue import fail_expired_stalled

    async with worker_db() as s:
        s.add(Event(  # parked long ago — should give up
            type=EventType.THREAD_READY, status=EventStatus.STALLED, attempts=0,
            available_at=datetime.now(UTC) - timedelta(hours=48),
            payload={"last_error": "PermissionDeniedError('403 Forbidden')"},
        ))
        s.add(Event(  # parked just now — should keep waiting
            type=EventType.THREAD_READY, status=EventStatus.STALLED, attempts=0,
            available_at=datetime.now(UTC),
            payload={"last_error": "PermissionDeniedError('403 Forbidden')"},
        ))
        await s.commit()

    async with worker_db() as s:
        assert await fail_expired_stalled(s, timedelta(hours=24)) == 1
        await s.commit()

    async with worker_db() as s:
        by_status = {}
        for ev in (await s.execute(select(Event))).scalars().all():
            by_status[ev.status] = by_status.get(ev.status, 0) + 1
        assert by_status.get(EventStatus.FAILED) == 1
        assert by_status.get(EventStatus.STALLED) == 1
        failed = (await s.execute(
            select(Event).where(Event.status == EventStatus.FAILED)
        )).scalars().first()
        assert "gave up" in failed.payload["last_error"]


async def test_idle_sweep_queues_memory_capture_only_where_it_is_on(worker_db):
    """The facts sweep (memory.capture) is a switch the owner flips per space: off
    everywhere means the sweep queues nothing, and nothing ever calls a model."""
    from email_reader.memory import capture
    from tests.conftest import SPACE_ID, mk_space

    async with worker_db() as s:
        await mk_space(s, 1)
        await mk_space(s, 2)
        await s.commit()
    await worker._maybe_sweep_facts()
    async with worker_db() as s:
        assert (await s.execute(select(Event))).scalars().all() == []
        await capture.set_on(s, SPACE_ID, True)
        await s.commit()
    await worker._maybe_sweep_facts()
    await worker._maybe_sweep_facts()  # the queued one is still open: no second event
    async with worker_db() as s:
        events = (await s.execute(select(Event))).scalars().all()
        assert [(e.type, e.payload["space_id"]) for e in events] == [
            (EventType.FACTS_CAPTURE, str(SPACE_ID))
        ]


async def test_out_of_money_parks_instead_of_burning_the_retries(worker_db, monkeypatch):
    """402 used to cost an event its five attempts in ninety seconds, and a whole batch of
    meetings died before anyone could top up. After a top-up the same endpoint answers."""
    import httpx
    import openai

    async with worker_db() as s:
        await enqueue(s, EventType.MEETING_READY, {"meeting_id": "m1"})
        await s.commit()

    async def broke(session, payload):
        req = httpx.Request("POST", "https://routerai.ru/api/v1/chat/completions")
        raise openai.APIStatusError(
            "Error code: 402 - {'error': 'Insufficient balance'}",
            response=httpx.Response(402, request=req), body={"error": "Insufficient balance"},
        )

    monkeypatch.setattr(worker, "_handlers", lambda: {EventType.MEETING_READY: broke})
    assert await worker.process_one() is True

    async with worker_db() as s:
        ev = (await s.execute(select(Event))).scalars().first()
        assert ev.status == EventStatus.STALLED and ev.attempts == 0


def test_a_stored_402_is_waited_out_but_a_bad_key_is_not():
    from email_reader.ai.errors import message_is_model_unavailable

    assert message_is_model_unavailable(
        "APIStatusError(\"Error code: 402 - {'error': 'Insufficient balance'}\")"
    )
    assert not message_is_model_unavailable("AuthenticationError('Error code: 401')")


async def test_the_health_check_wakes_only_the_work_whose_model_answers(worker_db, monkeypatch):
    """The check used to ask only the default model: once it answered, every parked event
    went back to the queue — including those waiting on another space's provider that was
    out of money. They hit the same 402 every minute and never aged out."""
    from datetime import UTC, datetime, timedelta

    from email_reader import meetings, settings_store
    from email_reader import model_connections as mc
    from email_reader.ai import extractor
    from email_reader.models import Account, Meeting, Thread
    from tests.conftest import mk_space

    async with worker_db() as s:
        work, home = await mk_space(s, 1), await mk_space(s, 2)
        local = await mc.create_connection(
            s, name="Локальная", provider="openai_custom", base_url="http://127.0.0.1:8080/v1",
            api_key="local", model="local-model",
        )
        routerai = await mc.create_connection(
            s, name="RouterAI", provider="openai_custom", base_url="https://routerai.ru/api/v1",
            api_key="k", model="deepseek",
        )
        work.model_connection_id, home.model_connection_id = local.id, routerai.id
        thread_of = {}
        for sp in (work, home):
            a = Account(space_id=sp.id, name=sp.name, provider="yandex", host="h", port=993,
                        login=f"{sp.sort}@y.ru", password_enc="x", folders=["INBOX"])
            s.add(a)
            await s.flush()
            t = Thread(account_id=a.id)
            s.add(t)
            await s.flush()
            thread_of[sp.sort] = str(t.id)
        # A meeting of the broke space, but meetings are read by the local model now.
        m = Meeting(ext_id="rec1", space_id=home.id, started_at=datetime.now(UTC))
        s.add(m)
        await settings_store.set_value(s, meetings.MODEL_KEY, str(local.id))
        await s.flush()

        long_ago = datetime.now(UTC) - timedelta(hours=48)

        def parked(type_, payload, since=None):
            ev = Event(type=str(type_), payload=payload, status=EventStatus.STALLED,
                       available_at=since or datetime.now(UTC))
            s.add(ev)
            return ev

        work_mail = parked(EventType.THREAD_READY, {"thread_id": thread_of[1]}, long_ago)
        home_mail = parked(EventType.THREAD_READY, {"thread_id": thread_of[2]})
        home_old = parked(EventType.PROJECT_MATCH, {"thread_id": thread_of[2]}, long_ago)
        meeting = parked(EventType.MEETING_READY, {"meeting_id": str(m.id)})
        await s.commit()

    pings: list[str] = []

    async def ping(*, kind, base_url, api_key, model, no_thinking=False):
        pings.append(base_url)
        return None if "8080" in base_url else "Error code: 402 - Insufficient balance"

    monkeypatch.setattr(extractor, "test_connection", ping)
    await worker._maybe_revive_stalled()

    assert sorted(pings) == ["http://127.0.0.1:8080/v1", "https://routerai.ru/api/v1"]
    async with worker_db() as s:
        status = {ev.id: ev.status for ev in (await s.execute(select(Event))).scalars()}
    assert status[work_mail.id] == EventStatus.PENDING  # its model answers, age aside
    assert status[meeting.id] == EventStatus.PENDING    # read by the model picked for meetings
    assert status[home_mail.id] == EventStatus.STALLED  # its provider still says 402
    assert status[home_old.id] == EventStatus.FAILED    # …and past the ceiling it gives up


async def test_a_failure_already_redone_is_not_revived(worker_db):
    """Nine meetings failed on 402, the owner pressed «Разобрать» again, and the new events
    routed them. Reviving the old failures at the next start would route them twice."""
    from email_reader.queue import reclassify_failed_as_stalled

    no_money = "APIStatusError(\"Error code: 402 - {'error': 'Insufficient balance'}\")"
    async with worker_db() as s:
        redone = Event(type=EventType.MEETING_READY, status=EventStatus.FAILED, attempts=5,
                       payload={"meeting_id": "m1", "last_error": no_money})
        lone = Event(type=EventType.MEETING_READY, status=EventStatus.FAILED, attempts=5,
                     payload={"meeting_id": "m2", "last_error": no_money})
        s.add_all([redone, lone])
        await s.flush()
        s.add(Event(type=EventType.MEETING_READY, status=EventStatus.DONE,
                    payload={"meeting_id": "m1"}))
        await s.commit()

    async with worker_db() as s:
        assert await reclassify_failed_as_stalled(s) == 1
        await s.commit()

    async with worker_db() as s:
        status = {ev.id: ev.status for ev in (await s.execute(select(Event))).scalars()}
    assert status[redone.id] == EventStatus.FAILED
    assert status[lone.id] == EventStatus.STALLED
