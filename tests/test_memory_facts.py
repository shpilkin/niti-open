"""Facts: the rows behind the space profile and a person's notes — what they render back
into, and what stays on disk when they change."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from email_reader import profiles
from email_reader.memory import facts
from email_reader.memory import people as people_svc
from email_reader.models import MemoryFact
from tests.conftest import mk_space

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
TEXT = "Роль: PM.\nКоманды:\n  - Аналитика (Оля) — метрики."


async def _rows(s, space_id=None, subject="me"):
    q = select(MemoryFact).where(MemoryFact.subject_kind == subject)
    q = q.where(MemoryFact.space_id == space_id) if space_id else q
    return list((await s.execute(q.order_by(MemoryFact.created_at, MemoryFact.seq))).scalars())


async def test_the_profile_reads_back_as_written_and_remembered_lines_follow_it(db):
    async with db() as s:
        sp = await mk_space(s)
        await profiles.set_profile(s, sp.id, TEXT + "  \n")
        assert await profiles.get_profile(s, sp.id) == TEXT
        await profiles.remember_line(s, sp.id, "счета шлю в бухгалтерию", now=NOW)
        assert await profiles.get_profile(s, sp.id) == (
            TEXT + "\n- 2026-09-05: счета шлю в бухгалтерию"
        )
        # The editor sees its own text only — a remembered line never folds into it.
        assert await profiles.core(s, sp.id) == TEXT
        await s.commit()


async def test_editing_the_profile_retires_the_old_text_and_keeps_what_was_remembered(db):
    async with db() as s:
        sp = await mk_space(s)
        await profiles.set_profile(s, sp.id, TEXT)
        await profiles.remember_line(s, sp.id, "счета шлю в бухгалтерию", now=NOW)
        await profiles.set_profile(s, sp.id, TEXT)  # unchanged: no new row
        assert len(await _rows(s, sp.id)) == 2
        await profiles.set_profile(s, sp.id, "Роль: CTO.")
        await s.commit()
        assert await profiles.get_profile(s, sp.id) == (
            "Роль: CTO.\n- 2026-09-05: счета шлю в бухгалтерию"
        )
        rows = await _rows(s, sp.id)
        old = next(r for r in rows if r.text == TEXT)
        new = next(r for r in rows if r.text == "Роль: CTO.")
    assert old.valid_to is not None and old.status == "active"  # kept, just not current
    assert new.supersedes_id == old.id and new.valid_to is None
    assert len(rows) == 3


async def test_a_remembered_line_can_be_forgotten_one_at_a_time(db):
    async with db() as s:
        sp = await mk_space(s)
        await profiles.set_profile(s, sp.id, "Роль: PM.")
        await profiles.remember_line(s, sp.id, "первое", now=NOW)
        second = await profiles.remember_line(s, sp.id, "второе", now=NOW)
        assert await profiles.forget(s, sp.id, second.id) is True
        assert await profiles.forget(s, sp.id, second.id) is False   # already gone
        assert await profiles.get_profile(s, sp.id) == "Роль: PM.\n- 2026-09-05: первое"
        other = await mk_space(s, 2)
        first = (await profiles.remembered(s, sp.id))[0]
        assert await profiles.forget(s, other.id, first.id) is False  # not that space's line
        await s.commit()


async def test_me_facts_stay_in_their_space(db):
    async with db() as s:
        work, home = await mk_space(s, 1), await mk_space(s, 2)
        await profiles.set_profile(s, work.id, "Роль: PM.")
        await profiles.set_profile(s, home.id, "Дача.")
        await profiles.remember_line(s, work.id, "счета в бухгалтерию", now=NOW)
        await s.commit()
        assert await profiles.get_profile(s, work.id) == (
            "Роль: PM.\n- 2026-09-05: счета в бухгалтерию"
        )
        assert await profiles.get_profile(s, home.id) == "Дача."
        assert await profiles.get_profile(s, None) == ""


async def test_notes_are_one_fact_per_line_and_unchanged_lines_keep_their_rows(db):
    async with db() as s:
        person = await people_svc.ensure_person(s, "petrov@romashka.ru", "Пётр")
        await people_svc.set_notes(s, person, "CTO Ромашки\n\nтехнику — Борису\n")
        assert await people_svc.notes_of(s, person) == "CTO Ромашки\nтехнику — Борису"
        before = {r.text: r.id for r in await _rows(s, subject="person")}
        await people_svc.set_notes(s, person, "CTO Ромашки\nвсё через Ивана")
        after = {r.text: r for r in await _rows(s, subject="person")}
        assert after["CTO Ромашки"].id == before["CTO Ромашки"]          # untouched line, same row
        assert after["технику — Борису"].valid_to is not None             # removed line, retired
        assert after["всё через Ивана"].valid_to is None
        # ...and the removed line is out of the search index, the kept one still in it
        from email_reader.search import query as search_query
        _, gone = await search_query.search(s, "Борису где:память")
        _, kept = await search_query.search(s, "Ивана где:память")
        assert gone == [] and [r.ref for r in kept] == [after["всё через Ивана"].id.hex]
        await people_svc.append_note(s, person, "ведёт поставки", now=NOW)
        assert await people_svc.notes_of(s, person) == (
            "CTO Ромашки\nвсё через Ивана\n2026-09-05: ведёт поставки"
        )
        await people_svc.set_notes(s, person, "")
        await s.commit()
        assert await people_svc.notes_of(s, person) == ""
        assert len(await _rows(s, subject="person")) == 4  # nothing was deleted


async def test_people_with_notes_and_the_notes_map_cover_only_current_facts(db):
    async with db() as s:
        a = await people_svc.ensure_person(s, "a@x.ru")
        b = await people_svc.ensure_person(s, "b@x.ru")
        c = await people_svc.ensure_person(s, "c@x.ru")
        await people_svc.set_notes(s, a, "первый")
        await people_svc.set_notes(s, b, "второй")
        await people_svc.set_notes(s, b, "")
        await s.commit()
        assert await facts.people_with_notes(s, [a.id, b.id, c.id]) == {a.id}
        assert await facts.notes_map(s) == {a.id.hex: "первый"}
        assert await facts.people_with_notes(s, []) == set()


async def test_a_merge_moves_the_facts_and_undo_moves_them_back(db):
    async with db() as s:
        await mk_space(s)
        keep = await people_svc.ensure_person(s, "ivan@x.ru", "Иванов")
        drop = await people_svc.ensure_person_by_name(s, "Иван Иванов", source="meeting")
        await people_svc.set_notes(s, keep, "пишет по утрам")
        await people_svc.set_notes(s, drop, "ведёт поставки\nлюбит сроки")
        drop_rows = {r.id for r in await _rows(s, subject="person") if r.subject_key == drop.id.hex}
        merged, undo = await people_svc.merge(s, keep.id, [drop.id])
        await s.commit()
        assert await people_svc.notes_of(s, merged) == "пишет по утрам\nведёт поставки\nлюбит сроки"
        assert {r["id"] for r in undo["dropped"][0]["facts"]} == {str(i) for i in drop_rows}
        await people_svc.unmerge(s, undo)
        await s.commit()
        assert await people_svc.notes_of(s, keep) == "пишет по утрам"
        back = await people_svc.get_person_by_name(s, "Иван Иванов")
        assert await people_svc.notes_of(s, back) == "ведёт поставки\nлюбит сроки"
