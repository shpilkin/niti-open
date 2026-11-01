"""The search index: what goes in, what comes out, and the parts that are easy to get
wrong — Russian word forms, ё/е, recipients, meeting moments, space scoping."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from email_reader.enums import ActionStatus, ActionType, Direction
from email_reader.memory import people as people_svc
from email_reader.models import (
    Account,
    ActionItem,
    CalendarEvent,
    Email,
    Meeting,
    Person,
    Project,
    Thread,
)
from email_reader.search import index as ix
from email_reader.search import query as q
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
TODAY = NOW.date()


async def _mail(s, acc, **kw):
    e = Email(account_id=acc.id, dedup_hash=uuid.uuid4().hex,
              direction=kw.pop("direction", Direction.INBOUND),
              from_addr=kw.pop("from_addr", "petrov@romashka.ru"),
              from_name=kw.pop("from_name", "Пётр Петров"),
              to_addrs=kw.pop("to_addrs", [{"name": "Я", "email": "me@b.ru"}]),
              cc_addrs=[], sent_at=kw.pop("sent_at", NOW - timedelta(days=1)), **kw)
    s.add(e)
    await s.flush()
    await ix.index_mail(s, e)
    return e


async def _space_setup(s, n=1):
    await mk_space(s, n)
    acc = Account(space_id=uuid.UUID(int=n), name=f"w{n}", host="h",
                  login=f"me{n}@b.ru", folders=[])
    s.add(acc)
    await s.flush()
    return acc


async def test_a_word_typed_in_full_finds_the_other_form(db):
    """The gap that made the old mail search miss: people type «договора», the letter
    says «договор». Stems bridge it; the exact form still ranks first."""
    async with db() as s:
        acc = await _space_setup(s)
        exact = await _mail(s, acc, subject="Договора на подпись", body_text="текст")
        other = await _mail(s, acc, subject="Про договор", body_text="и смета тоже")
        await s.commit()

        _, results = await q.search(s, "договора", space_id=SPACE_ID, today=TODAY)
        refs = [r.ref for r in results]
    assert exact.id.hex in refs and other.id.hex in refs
    assert refs[0] == exact.id.hex, "the form the user typed must rank above the stem hit"


async def test_yo_and_ye_are_the_same_letter(db):
    async with db() as s:
        acc = await _space_setup(s)
        mail = await _mail(s, acc, subject="Счёт на оплату", body_text="—")
        await s.commit()
        _, hits = await q.search(s, "счет", space_id=SPACE_ID, today=TODAY)
        _, back = await q.search(s, "счёт", space_id=SPACE_ID, today=TODAY)
    assert [r.ref for r in hits] == [mail.id.hex]
    assert [r.ref for r in back] == [mail.id.hex]


async def test_recipients_are_searchable_not_only_the_sender(db):
    """`sender` was the only address column of the old index — mail addressed to someone
    could not be found by their address at all."""
    async with db() as s:
        acc = await _space_setup(s)
        mail = await _mail(s, acc, subject="Смета", body_text="—",
                           to_addrs=[{"name": "Анна Кузнецова", "email": "anna@x.ru"}])
        await s.commit()
        _, by_addr = await q.search(s, "anna@x.ru", space_id=SPACE_ID, today=TODAY)
        _, by_name = await q.search(s, "Кузнецова", space_id=SPACE_ID, today=TODAY)
    assert [r.ref for r in by_addr] == [mail.id.hex]
    assert [r.ref for r in by_name] == [mail.id.hex]


async def test_every_kind_lands_in_one_result_list(db):
    async with db() as s:
        acc = await _space_setup(s)
        await _mail(s, acc, subject="Тендер по смете", body_text="—")
        item = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY,
                          title="Ответить по тендеру", status=ActionStatus.OPEN,
                          counterparty="Пётр", reason="сроки")
        meeting = Meeting(ext_id="m1", space_id=SPACE_ID, started_at=NOW - timedelta(days=2),
                          title="Планёрка", summary="Обсудили тендер", participants=["Пётр"])
        event = CalendarEvent(account_id=acc.id, provider_uid="e1", start_at=NOW,
                              title="Тендер: защита", description="—")
        person = Person(name="Пётр Петров", email="petrov@romashka.ru")
        project = Project(name="Тендер-2026", space_id=SPACE_ID, prompt="")
        s.add_all([item, meeting, event, person, project])
        await s.flush()
        await people_svc.set_notes(s, person, "тендеры")  # indexes the fact and the person
        for obj, fn in ((item, ix.index_card), (meeting, ix.index_meeting),
                        (event, ix.index_event), (project, ix.index_project)):
            await fn(s, obj)
        await s.commit()

        _, results = await q.search(s, "тендер", space_id=SPACE_ID, today=TODAY)
        kinds = {r.kind for r in results}
        groups = q.grouped(results)
    assert kinds == {"mail", "card", "meeting", "event", "person", "project", "fact"}
    assert [g[0] for g in groups] == [
        "mail", "card", "meeting", "event", "person", "project", "fact",
    ]


async def test_meeting_moments_are_indexed_with_their_timecode(db, tmp_path):
    transcript = tmp_path / "t.txt"
    transcript.write_text(
        "[00:00:01] Спикер 1: Начали.\n"
        "[00:08:23] Спикер 2: По смете договорились перенести оплату на октябрь.\n",
        encoding="utf-8",
    )
    async with db() as s:
        await mk_space(s)
        meeting = Meeting(ext_id="m1", space_id=SPACE_ID, started_at=NOW,
                          title="Планёрка", transcript_path=str(transcript))
        s.add(meeting)
        await s.flush()
        await ix.index_meeting(s, meeting)
        await s.commit()

        _, results = await q.search(s, "перенести оплату", space_id=SPACE_ID, today=TODAY)
        turns = [r for r in results if r.kind == "turn"]
    assert turns, "the moment must be findable on its own"
    assert turns[0].url == f"/meeting/{meeting.id}?at=[00:08:23]"
    assert q.HL_ON in turns[0].snippet


async def test_results_stay_inside_the_current_space(db):
    async with db() as s:
        work = await _space_setup(s, 1)
        home = await _space_setup(s, 2)
        mine = await _mail(s, work, subject="Смета для Ромашки", body_text="—")
        await _mail(s, home, subject="Смета на ремонт", body_text="—")
        await s.commit()
        _, here = await q.search(s, "смета", space_id=SPACE_ID, today=TODAY)
        _, there = await q.search(s, "смета", space_id=SPACE_ID_2, today=TODAY)
    assert [r.ref for r in here] == [mine.id.hex]
    assert len(there) == 1 and there[0].ref != mine.id.hex


async def test_reconcile_adds_what_a_writer_forgot_and_drops_what_is_gone(db):
    async with db() as s:
        acc = await _space_setup(s)
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()
        # Written straight to the store, as a forgotten call site would.
        orphan = Email(account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
                       direction=Direction.INBOUND, from_addr="x@y.ru",
                       to_addrs=[], cc_addrs=[], subject="Неучтённая смета",
                       body_text="—", sent_at=NOW)
        s.add(orphan)
        await s.commit()

        _, before = await q.search(s, "неучтённая", space_id=SPACE_ID, today=TODAY)
        assert before == []

        changed = await ix.reconcile(s)
        await s.commit()
        _, after = await q.search(s, "неучтённая", space_id=SPACE_ID, today=TODAY)
        assert changed["added"] == 1
        assert [r.ref for r in after] == [orphan.id.hex]

        await s.execute(text("delete from emails where id = :i"), {"i": orphan.id.hex})
        await s.commit()
        changed = await ix.reconcile(s)
        await s.commit()
        _, gone = await q.search(s, "неучтённая", space_id=SPACE_ID, today=TODAY)
    assert changed["removed"] == 1
    assert gone == []


async def test_the_first_index_build_goes_in_batches(db, monkeypatch):
    """Одной транзакцией это держало замок базы 13 с на живом архиве
    (docs/feature-storage-plan.md, этап 1)."""
    from contextlib import asynccontextmanager

    from email_reader.pipeline import worker

    transactions = 0

    @asynccontextmanager
    async def scope():
        nonlocal transactions
        transactions += 1
        async with db() as s:
            yield s
            await s.commit()

    monkeypatch.setattr(worker, "session_scope", scope)
    monkeypatch.setattr(ix, "BUILD_BATCH", 1)
    rounds = []
    real_reconcile = ix.reconcile

    async def counting(session, *, limit=500):
        rounds.append(limit)
        return await real_reconcile(session, limit=limit)

    monkeypatch.setattr(ix, "reconcile", counting)

    async with db() as s:
        acc = await _space_setup(s)
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()
        # Прямо в хранилище, мимо индекса: так база выглядит сразу после миграции.
        for subject in ("Смета", "Договор", "Счёт"):
            s.add(Email(account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
                        direction=Direction.INBOUND, from_addr="x@y.ru", to_addrs=[],
                        cc_addrs=[], subject=subject, body_text="—", sent_at=NOW))
        await s.commit()
        assert await ix.counts(s) == {}

    await worker._maybe_sweep_search()

    async with db() as s:
        counts = await ix.counts(s)
        _, found = await q.search(s, "договор", space_id=SPACE_ID, today=TODAY)
    assert counts["mail"] == 3
    assert len(found) == 1
    assert rounds == [1, 1, 1, 1]  # по письму за круг и последний, пустой
    assert transactions > len(rounds)  # каждый круг — своя транзакция, плюс очистка


async def test_rebuild_clears_and_refills_in_batches(db, monkeypatch):
    """Кнопка «Переиндексировать» и CLI: та же пересборка, пачками
    (docs/feature-storage-plan.md, этап 1)."""
    from contextlib import asynccontextmanager

    monkeypatch.setattr(ix, "BUILD_BATCH", 1)
    rounds = []
    real_reconcile = ix.reconcile

    async def counting(session, *, limit=500):
        rounds.append(limit)
        return await real_reconcile(session, limit=limit)

    monkeypatch.setattr(ix, "reconcile", counting)

    @asynccontextmanager
    async def scope():
        async with db() as s:
            yield s
            await s.commit()

    async with db() as s:
        acc = await _space_setup(s)
        for subject in ("Смета", "Договор", "Счёт"):
            await _mail(s, acc, subject=subject, body_text="—")
        # Документ о письме, которого давно нет: пересборка должна его убрать.
        await ix.put(s, [ix.Doc(kind="mail", ref=uuid.uuid4().hex, at=NOW,
                                title="Привидение", body="—")])
        await s.commit()

    added = await ix.rebuild(scope)

    async with db() as s:
        counts = await ix.counts(s)
        _, ghost = await q.search(s, "привидение", space_id=SPACE_ID, today=TODAY)
        _, found = await q.search(s, "договор", space_id=SPACE_ID, today=TODAY)
    assert counts["mail"] == 3 and added == 3
    assert ghost == [] and len(found) == 1
    assert rounds == [1, 1, 1, 1]  # по письму за круг и последний, пустой


async def test_reindex_rebuilds_everything(db):
    async with db() as s:
        acc = await _space_setup(s)
        await _mail(s, acc, subject="Смета", body_text="—")
        await s.commit()
        written = await ix.reindex(s)
        await s.commit()
        counts = await ix.counts(s)
    assert written["mail"] == 1
    assert counts["mail"] == 1


async def test_a_base_form_finds_an_inflected_one_even_when_the_stemmer_disagrees(db):
    """Snowball is not idempotent: «чемодане» is indexed as «чемодан», while the base
    form «чемодан» stems on to «чемода». Real gap — the
    word as typed is looked for in the stem column too."""
    from email_reader.search.text import stem

    assert stem("чемодане") == "чемодан" and stem("чемодан") != "чемодан"

    async with db() as s:
        acc = await _space_setup(s)
        mail = await _mail(s, acc, subject="Заметки", body_text="храню всё в чемодане")
        await s.commit()
        _, found = await q.search(s, "чемодан", space_id=SPACE_ID, today=TODAY)
    assert [r.ref for r in found] == [mail.id.hex]


# --- Facts: the owner's memory in the index -------------------------------------------


async def test_a_fact_is_indexed_when_written_and_leaves_when_retired(db):
    from email_reader import profiles
    from email_reader.memory import people as people_svc

    async with db() as s:
        await _space_setup(s)
        line = await profiles.remember_line(s, SPACE_ID, "счета от Ромашки — в бухгалтерию")
        petrov = await people_svc.ensure_person(s, "petrov@romashka.ru", "Пётр Петров")
        await people_svc.set_notes(s, petrov, "CTO Ромашки, технику — Борису")
        await s.commit()

        _, results = await q.search(s, "бухгалтерия", space_id=SPACE_ID, today=TODAY)
        mine = [r for r in results if r.kind == "fact"]
        assert [r.ref for r in mine] == [line.id.hex]
        assert mine[0].url == "/settings/section/profile" and mine[0].who == ""

        _, results = await q.search(s, "Борису", space_id=SPACE_ID, today=TODAY)
        theirs = [r for r in results if r.kind == "fact"]
        assert len(theirs) == 1 and theirs[0].who == "Пётр Петров petrov@romashka.ru"
        assert theirs[0].url == "/person?q=petrov@romashka.ru" and theirs[0].pane
        # ...and by the person's name, which is not in the text of the fact itself
        _, results = await q.search(s, "Петров где:память", space_id=SPACE_ID, today=TODAY)
        assert [r.ref for r in results] == [theirs[0].ref]

        assert await profiles.forget(s, SPACE_ID, line.id)
        await s.commit()
        _, results = await q.search(s, "бухгалтерия", space_id=SPACE_ID, today=TODAY)
        assert [r for r in results if r.kind == "fact"] == []
        # the retired row is not «missing» to the sweeps either
        assert (await ix.reconcile(s)) == {"added": 0, "removed": 0}
        written = await ix.reindex(s, "fact")
    assert written == {"fact": 1}


async def test_my_facts_stay_in_their_space_but_a_persons_are_global(db):
    from email_reader import profiles

    async with db() as s:
        await _space_setup(s, 1)
        await _space_setup(s, 2)
        await profiles.remember_line(s, SPACE_ID, "тендер согласую с Катей")
        await s.commit()
        _, here = await q.search(s, "тендер", space_id=SPACE_ID, today=TODAY)
        _, there = await q.search(s, "тендер", space_id=SPACE_ID_2, today=TODAY)
    assert [r.kind for r in here] == ["fact"]
    assert there == []


# --- уточнения: «от:», «кому:», «статус:», «есть:» (docs/feature-search-mcp-agent.md §9) --


async def test_from_and_to_are_not_the_same_question(db):
    """«от:Петров» и «кому:Петров» давали почти один и тот же экран: направление
    проверялось только у писем, а карточки, встречи и события проходили насквозь."""
    async with db() as s:
        acc = await _space_setup(s)
        his = await _mail(s, acc, subject="Смета", body_text="—")
        mine = await _mail(s, acc, subject="Ответ по смете", body_text="—",
                           direction=Direction.OUTBOUND, from_addr="me1@b.ru",
                           from_name="Я", to_addrs=[{"name": "Пётр Петров",
                                                     "email": "petrov@romashka.ru"}])
        card = ActionItem(space_id=SPACE_ID, type=ActionType.TO_DELEGATE,
                          title="Смета: передать", status=ActionStatus.OPEN,
                          counterparty="Анна Кузнецова", delegate_to="Пётр Петров")
        ev = CalendarEvent(account_id=acc.id, provider_uid="ev-1", title="Смета: статус",
                           start_at=NOW, organizer="anna@x.ru",
                           attendees=[{"name": "Пётр Петров",
                                       "email": "petrov@romashka.ru"}])
        s.add_all([card, ev])
        await s.flush()
        await ix.index_card(s, card)
        await ix.index_event(s, ev)
        await s.commit()

        _, frm = await q.search(s, "от:Петров", space_id=SPACE_ID, today=TODAY)
        _, to = await q.search(s, "кому:Петров", space_id=SPACE_ID, today=TODAY)
    # От него — только его письмо. Ему — письмо к нему, карточка, переданная ему, и
    # встреча, на которую его позвали.
    assert {r.ref for r in frm} == {his.id.hex}
    names = {mine.id.hex: "письмо к нему", card.id.hex: "карточка",
             ev.id.hex: "встреча"}
    assert {names.get(r.ref, r.ref) for r in to} == set(names.values())


async def test_a_letter_where_the_person_is_only_in_copy_answers_to_whom(db):
    """`кому:` читал только `to_addrs`, и письмо с человеком в копии исчезало, хотя в
    индексе копия есть."""
    async with db() as s:
        acc = await _space_setup(s)
        copied = await _mail(s, acc, subject="Смета", body_text="—",
                             to_addrs=[{"name": "Я", "email": "me1@b.ru"}])
        copied.cc_addrs = [{"name": "Анна Кузнецова", "email": "anna@x.ru"}]
        await s.flush()
        await ix.index_mail(s, copied)
        await s.commit()
        _, found = await q.search(s, "кому:Кузнецова", space_id=SPACE_ID, today=TODAY)
    assert [r.ref for r in found] == [copied.id.hex]


async def test_a_filter_narrows_to_the_kinds_it_can_judge(db):
    """«статус:» знает только карточка, «есть:вложение» — только письмо. Раньше оба
    уточнения пропускали чужие виды нетронутыми: запрос про открытые обязательства
    отвечал страницей писем."""
    async with db() as s:
        acc = await _space_setup(s)
        await _mail(s, acc, subject="Смета", body_text="—", has_attachments=False)
        withfile = await _mail(s, acc, subject="Смета с файлом", body_text="—",
                               has_attachments=True)
        open_card = ActionItem(space_id=SPACE_ID, type=ActionType.TASK,
                               title="Смета: собрать", status=ActionStatus.OPEN)
        done_card = ActionItem(space_id=SPACE_ID, type=ActionType.TASK,
                               title="Смета: сдать", status=ActionStatus.DONE)
        s.add_all([open_card, done_card])
        await s.flush()
        for c in (open_card, done_card):
            await ix.index_card(s, c)
        await s.commit()

        _, open_only = await q.search(s, "статус:открыт смета", space_id=SPACE_ID,
                                      today=TODAY)
        _, with_file = await q.search(s, "есть:вложение смета", space_id=SPACE_ID,
                                      today=TODAY)
        # Противоречивое уточнение отвечает пустотой, а не «чем нашлось».
        _, nonsense = await q.search(s, "есть:вложение где:обязательства смета",
                                     space_id=SPACE_ID, today=TODAY)
    assert [r.ref for r in open_only] == [open_card.id.hex]
    assert [r.ref for r in with_file] == [withfile.id.hex]
    assert nonsense == []
