"""Global person memory: one `Person` row per normalized address, notes the user writes
(kept as facts since docs/feature-memory-facts.md — read back through `notes_of`)."""

from __future__ import annotations

from sqlalchemy import select

from email_reader.enums import Direction
from email_reader.memory import people as people_svc
from email_reader.models import Email, Person


async def test_ensure_person_is_canonical_and_idempotent(db):
    async with db() as s:
        p1 = await people_svc.ensure_person(s, " Petrov@Romashka.RU ", "Пётр Петров")
        p2 = await people_svc.ensure_person(s, "petrov@romashka.ru")
        await s.commit()
        rows = (await s.execute(select(Person))).scalars().all()
    assert p1.id == p2.id
    assert len(rows) == 1
    assert rows[0].email == "petrov@romashka.ru"
    assert rows[0].name == "Пётр Петров"


async def test_set_notes_stores_and_clears(db):
    async with db() as s:
        person = await people_svc.ensure_person(s, "petrov@romashka.ru")
        p = await people_svc.set_notes(s, person, "  CTO Ромашки, технику — Борису  ")
        await s.commit()
        assert await people_svc.notes_of(s, p) == "CTO Ромашки, технику — Борису"
        assert p.updated_at is not None
        p = await people_svc.set_notes(s, person, "   ")
        await s.commit()
        cleared = await people_svc.notes_of(s, p)
    assert cleared == ""


async def test_get_person_unknown_or_empty(db):
    async with db() as s:
        assert await people_svc.get_person(s, "nobody@x.ru") is None
        assert await people_svc.get_person(s, "") is None


def _inbound(addr, name=None):
    return Email(direction=Direction.INBOUND, from_addr=addr, from_name=name)


def test_resolve_address_prefers_the_query_address():
    emails = [_inbound("petrov@romashka.ru", "Пётр"), _inbound("other@x.ru")]
    assert people_svc.resolve_address("Petrov@Romashka.ru", emails) == (
        "petrov@romashka.ru", "Пётр"
    )


def test_resolve_address_from_a_name_uses_the_most_frequent_sender():
    emails = [
        _inbound("petrov@romashka.ru"), _inbound("petrov@romashka.ru"),
        _inbound("assistant@romashka.ru"),
        Email(direction=Direction.OUTBOUND, from_addr="me@b.ru"),
    ]
    assert people_svc.resolve_address("Пётр Петров", emails) == (
        "petrov@romashka.ru", "Пётр Петров"
    )


def test_resolve_address_without_mail_has_no_address():
    assert people_svc.resolve_address("Пётр", []) == (None, "Пётр")
