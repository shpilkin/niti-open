"""«Люди»: один список отовсюду, профиль под строкой и объединение личностей.

Проверяется то, ради чего фича делалась: человека со встречи можно запомнить, не имея его
адреса; строки из почты, встречи и карточки склеиваются в один профиль и разъединяются
обратно; граница пространства при этом не течёт (docs/feature-people.md).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from email_reader.memory import directory as dir_svc
from email_reader.memory import people as people_svc
from email_reader.models import ActionItem, Meeting, Person, PersonIdentity
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _item(counterparty, *, space_id=SPACE_ID, status="open", source_kind="mail", days=0):
    return ActionItem(
        space_id=space_id, type="MY_COMMITMENT", title="что-то", status=status,
        counterparty=counterparty, source_kind=source_kind, created_at=NOW - timedelta(days=days),
    )


# --- Identity parsing ---------------------------------------------------------------


def test_identity_of_reads_an_address_a_name_and_refuses_the_rest():
    assert people_svc.identity_of("Анна Белова (anna@x.ru)") == ("email", "anna@x.ru")
    assert people_svc.identity_of("  IVAN@x.ru ") == ("email", "ivan@x.ru")
    assert people_svc.identity_of("Иван  Иванов") == ("name", "иван иванов")
    # Too short to be anybody in particular, and a diarization placeholder.
    assert people_svc.identity_of("Ян") is None
    assert people_svc.identity_of("Спикер 2") is None
    assert people_svc.identity_of("") is None


# --- The list ------------------------------------------------------------------------


async def test_the_list_gathers_mail_meetings_and_cards_of_this_space_only(db):
    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        s.add_all([
            _item("Анна Белова (anna@x.ru)"),
            _item("Анна Белова (anna@x.ru)", status="done", days=3),
            _item("Иван Иванов", source_kind="meeting"),
            _item("Дачник (dacha@x.ru)", space_id=SPACE_ID_2),
        ])
        s.add(Meeting(ext_id="m1", space_id=SPACE_ID, started_at=NOW,
                      participants=["Пётр Петров", "Спикер 1"]))
        await s.commit()
        rows = await dir_svc.list_people(s, SPACE_ID)

    by_name = {r.name: r for r in rows}
    assert set(by_name) == {"Анна Белова", "Иван Иванов", "Пётр Петров"}
    anna = by_name["Анна Белова"]
    assert anna.emails == ["anna@x.ru"]
    assert (anna.open_items, anna.total_items) == (1, 2)
    assert by_name["Иван Иванов"].name_only and "meeting" in by_name["Иван Иванов"].sources
    # «Спикер 1» — не человек, а заглушка диаризации; чужое пространство — не наше дело.
    assert "Спикер 1" not in by_name and "Дачник" not in by_name


async def test_a_persons_own_addresses_are_not_in_their_own_list(db):
    from email_reader import identities as identities_svc

    async with db() as s:
        await mk_space(s)
        await identities_svc.add_identity(s, "me@x.ru")
        s.add_all([_item("me@x.ru"), _item("Кто-то (other@x.ru)")])
        await s.commit()
        rows = await dir_svc.list_people(s, SPACE_ID)
    assert [r.emails for r in rows] == [["other@x.ru"]]


async def test_search_finds_a_sender_who_never_made_a_card(db):
    from email_reader.models import Account, Email, Thread

    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@x.ru", folders=[])
        s.add(acc)
        await s.flush()
        thread = Thread(account_id=acc.id)
        s.add(thread)
        await s.flush()
        s.add(Email(account_id=acc.id, thread_id=thread.id, rfc_message_id="<1>",
                    dedup_hash="h1", direction="inbound", from_addr="quiet@x.ru",
                    from_name="Тихий Читатель",
                    to_addrs=[], cc_addrs=[], subject="привет", body_text="т", sent_at=NOW))
        await s.commit()
        assert await dir_svc.list_people(s, SPACE_ID) == []
        found = await dir_svc.list_people(s, SPACE_ID, "Тихий")
    assert [r.emails for r in found] == [["quiet@x.ru"]]


# --- Profiles without an address -----------------------------------------------------


async def test_a_person_met_only_at_a_meeting_can_be_remembered(db):
    async with db() as s:
        await mk_space(s)
        person = await people_svc.ensure_person_by_name(s, "Иван Иванов", source="meeting")
        await people_svc.set_notes(s, person, "Главный по поставкам")
        await s.commit()
        again = await people_svc.get_person_by_name(s, "иван  иванов")
        notes = await people_svc.notes_of(s, again) if again else None
    assert person.email is None
    assert again is not None and notes == "Главный по поставкам"


async def test_a_placeholder_speaker_never_becomes_a_profile(db):
    async with db() as s:
        await mk_space(s)
        assert await people_svc.ensure_person_by_name(s, "Спикер 1") is None
        assert await people_svc.ensure_person_by_name(s, "Ян") is None


# --- Merge ---------------------------------------------------------------------------


async def _three_of_the_same_person(s):
    mail = await people_svc.ensure_person(s, "ivan@x.ru", "Иванов И.")
    await people_svc.set_notes(s, mail, "пишет по утрам")
    meet = await people_svc.ensure_person_by_name(s, "Иван Иванов", source="meeting")
    await people_svc.set_notes(s, meet, "ведёт поставки")
    task = await people_svc.ensure_person_by_name(s, "Ваня из Ромашки", source="manual")
    await s.flush()
    return mail, meet, task


async def test_merge_keeps_every_identity_and_both_halves_of_the_notes(db):
    async with db() as s:
        await mk_space(s)
        mail, meet, task = await _three_of_the_same_person(s)
        merged, undo = await people_svc.merge(s, mail.id, [meet.id, task.id])
        await s.commit()

        assert merged.id == mail.id
        idents = await people_svc.identities_of(s, merged.id)
        assert {(i.kind, i.value) for i in idents} == {
            ("email", "ivan@x.ru"), ("name", "иван иванов"), ("name", "ваня из ромашки"),
        }
        # Both halves are there, the survivor's first; where a line came from is on the
        # fact row now (evidence + the merge payload), not a header in the text.
        assert await people_svc.notes_of(s, merged) == "пишет по утрам\nведёт поставки"
        assert await s.get(Person, meet.id) is None
        assert len(undo["dropped"]) == 2
        # Любая из личностей теперь ведёт в один профиль.
        assert (await people_svc.resolve(s, "Ваня из Ромашки")).id == merged.id
        assert (await people_svc.resolve(s, "Кто-то (ivan@x.ru)")).id == merged.id


async def test_unmerge_puts_the_profiles_back_as_they_were(db):
    async with db() as s:
        await mk_space(s)
        mail, meet, task = await _three_of_the_same_person(s)
        before = {
            p.id: await people_svc.snapshot(s, p) for p in (mail, meet, task)
        }
        _, undo = await people_svc.merge(s, mail.id, [meet.id, task.id])
        await s.commit()

        await people_svc.unmerge(s, undo)
        await s.commit()
        after = {}
        for pid in before:
            p = await s.get(Person, pid)
            assert p is not None, pid
            after[pid] = await people_svc.snapshot(s, p)

    # Выживший возвращается тоже: слияние переписало его заметки, и отмена, которая чинит
    # только поглощённых, оставила бы у него обе половины навсегда.
    for pid, snap in before.items():
        assert after[pid]["identities"] == snap["identities"], pid
        assert after[pid]["email"] == snap["email"], pid
        assert after[pid]["notes"] == snap["notes"], pid
        assert after[pid]["name"] == snap["name"], pid
        assert after[pid]["updated_at"] == snap["updated_at"], pid


async def test_merging_the_same_row_twice_is_a_no_op(db):
    async with db() as s:
        await mk_space(s)
        mail = await people_svc.ensure_person(s, "ivan@x.ru", "Иванов")
        await s.commit()
        merged, undo = await people_svc.merge(s, mail.id, [mail.id])
        await s.commit()
    assert merged.id == mail.id and undo == {}


async def test_detach_pulls_one_identity_into_its_own_profile(db):
    async with db() as s:
        await mk_space(s)
        mail, meet, _ = await _three_of_the_same_person(s)
        merged, _ = await people_svc.merge(s, mail.id, [meet.id])
        await s.commit()

        fresh = await people_svc.detach(s, merged.id, "name", "иван иванов")
        await s.commit()
        assert fresh is not None and fresh.id != merged.id
        assert (await people_svc.resolve(s, "Иван Иванов")).id == fresh.id
        assert (await people_svc.resolve(s, "ivan@x.ru")).id == merged.id
        # Последнюю личность отвязать нельзя — это переименование, а не разделение.
        assert await people_svc.detach(s, fresh.id, "name", "иван иванов") is None


async def test_an_address_belongs_to_exactly_one_person(db):
    async with db() as s:
        await mk_space(s)
        one = await people_svc.ensure_person(s, "ivan@x.ru", "Иванов")
        two = await people_svc.ensure_person_by_name(s, "Другой Человек")
        await s.commit()
        # Попытка присвоить чужой адрес не крадёт его молча.
        assert await people_svc.add_identity(s, two, "email", "ivan@x.ru") is None
        rows = (await people_svc.identities_of(s, one.id))
        assert [(r.kind, r.value) for r in rows] == [("email", "ivan@x.ru")]


async def test_the_list_shows_a_merged_person_as_one_row(db):
    async with db() as s:
        await mk_space(s)
        s.add_all([_item("Иванов И. (ivan@x.ru)"), _item("Иван Иванов", source_kind="meeting")])
        await s.commit()
        assert len(await dir_svc.list_people(s, SPACE_ID)) == 2

        mail = await people_svc.ensure_person(s, "ivan@x.ru", "Иванов И.")
        meet = await people_svc.ensure_person_by_name(s, "Иван Иванов", source="meeting")
        await people_svc.merge(s, mail.id, [meet.id])
        await s.commit()

        rows = await dir_svc.list_people(s, SPACE_ID)
    assert len(rows) == 1
    row = rows[0]
    assert row.open_items == 2  # обе карточки — его
    assert {k for k, _, _ in row.identities} == {"email", "name"}
    assert row.sources == {"mail", "meeting"}


async def test_a_profile_id_survives_a_note_written_from_the_list(db):
    """Строка списка — производная; профиль появляется в тот момент, когда о человеке
    что-то записали, и с этого момента у строки есть постоянный ключ."""
    async with db() as s:
        await mk_space(s)
        s.add(_item("Иван Иванов", source_kind="meeting"))
        await s.commit()
        [row] = await dir_svc.list_people(s, SPACE_ID)
        assert row.key == "name:иван иванов" and row.person_id is None

        person = await people_svc.ensure_for(s, "Иван Иванов", source="meeting")
        await people_svc.set_notes(s, person, "поставки")
        await s.commit()
        [row] = await dir_svc.list_people(s, SPACE_ID)
    assert row.person_id == person.id and row.key == f"person:{person.id}"
    assert row.has_notes


async def test_identities_are_global_but_the_evidence_is_not(db):
    """Профиль один на все пространства (память о человеке глобальна), но в списке он
    появляется только там, где действительно что-то происходило."""
    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        s.add(_item("Иванов И. (ivan@x.ru)"))
        await s.commit()
        person = await people_svc.ensure_person(s, "ivan@x.ru", "Иванов И.")
        await people_svc.set_notes(s, person, "глобальная заметка")
        s.add(PersonIdentity(person_id=person.id, kind="name", value="иван иванов",
                             display="Иван Иванов", source="meeting"))
        await s.commit()

        here = await dir_svc.list_people(s, SPACE_ID)
        there = await dir_svc.list_people(s, SPACE_ID_2)
    assert [r.person_id for r in here] == [person.id]
    assert there == []
    # Чипы профиля показывают все его личности, включая ту, что в этом пространстве молчит.
    assert len(here[0].identities) == 2


async def test_merging_into_a_name_only_profile_carries_the_address(db):
    """Выживает профиль с заметками, даже если адрес — у второго: адрес уникален, поэтому
    его сначала надо отпустить и только потом присвоить (иначе UNIQUE на people.email)."""
    async with db() as s:
        await mk_space(s)
        meet = await people_svc.ensure_person_by_name(s, "Иван Иванов", source="meeting")
        await people_svc.set_notes(s, meet, "ведёт поставки")
        mail = await people_svc.ensure_person(s, "ivan@x.ru", "И. Иванов")
        await s.commit()

        merged, undo = await people_svc.merge(s, meet.id, [mail.id])
        await s.commit()
        assert merged.id == meet.id
        assert merged.email == "ivan@x.ru"
        assert (await people_svc.get_person(s, "ivan@x.ru")).id == meet.id

        await people_svc.unmerge(s, undo)
        await s.commit()
        back = await people_svc.get_person(s, "ivan@x.ru")
        assert back is not None and back.id == mail.id
        assert (await s.get(Person, meet.id)).email is None
