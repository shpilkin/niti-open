"""The write-lock watchdog: while a write transaction is open every other writer waits, so
one held too long is logged together with the code that started it."""

from __future__ import annotations

import asyncio
import logging

import pytest
from sqlalchemy import select

from email_reader import db as db_module
from email_reader.memory import people as people_svc
from email_reader.models import Person

LIMIT = 0.3


@pytest.fixture
def watched(db, monkeypatch, caplog):
    monkeypatch.setattr(db_module, "SLOW_WRITE_SECONDS", LIMIT)
    db_module._watch_writes(db.kw["bind"])
    caplog.set_level(logging.WARNING, logger="email_reader.db")
    return db


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "email_reader.db"]


async def test_a_write_held_past_the_limit_names_the_code_that_took_the_lock(watched, caplog):
    async with watched() as s:
        await people_svc.ensure_person(s, "petrov@romashka.ru")
        await asyncio.sleep(LIMIT * 3)  # a model call, a network round-trip
        await s.commit()

    [warning] = _warnings(caplog)
    assert warning.startswith("Write lock held ")
    assert "INSERT INTO people" in warning  # что пишут: автосброс уносит запись далеко от места
    assert "email_reader.memory.people:" in warning and " ensure_person" in warning


async def test_short_writes_and_long_reads_are_not_reported(watched, caplog):
    async with watched() as s:
        await people_svc.ensure_person(s, "petrov@romashka.ru")
        await s.commit()
    async with watched() as s:
        (await s.execute(select(Person))).scalars().all()
        await asyncio.sleep(LIMIT * 3)  # reads take no write lock
        await s.commit()

    assert _warnings(caplog) == []


async def test_a_write_that_was_rolled_back_does_not_age_the_next_one(watched, caplog):
    async with watched() as s:
        await people_svc.ensure_person(s, "petrov@romashka.ru")
        # left without commit: the session closes and rolls it back
    await asyncio.sleep(LIMIT * 3)
    async with watched() as s:
        await people_svc.ensure_person(s, "ivanov@romashka.ru")
        await s.commit()

    assert _warnings(caplog) == []
