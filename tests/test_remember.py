"""«Запомнить» from a card: a dated fact on the person or in the space profile, journaled."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import email_reader.api.app  # noqa: F401  — registers icon/age/local on the template env
from email_reader import profiles
from email_reader.api.common import templates
from email_reader.enums import ActionStatus, ActionType, Direction
from email_reader.memory import people as people_svc
from email_reader.memory import remember as remember_svc
from email_reader.models import Account, ActionItem, AgentDecision, Email, Identity, Thread
from tests.conftest import SPACE_ID, mk_space

ME = "me@b.ru"
THEM = "petrov@romashka.ru"
NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


async def _item_with_thread(s):
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name="acc", host="imap.b.ru", port=993, login=ME)
    s.add(acc)
    s.add(Identity(email=ME))
    await s.flush()
    th = Thread(account_id=acc.id)
    s.add(th)
    await s.flush()
    s.add(Email(
        account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
        direction=Direction.INBOUND, from_addr=THEM, from_name="Пётр",
        to_addrs=[{"email": ME, "name": None}], cc_addrs=[], sent_at=NOW,
    ))
    ai = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="Ответить",
                    status=ActionStatus.OPEN, source_thread_id=th.id)
    s.add(ai)
    await s.flush()
    return ai


async def test_counterparty_address_is_read_off_the_thread(db):
    async with db() as s:
        ai = await _item_with_thread(s)
        assert await remember_svc.counterparty_address(s, ai) == (THEM, "Пётр")


async def test_no_thread_no_address(db):
    async with db() as s:
        await mk_space(s)
        ai = ActionItem(
            space_id=SPACE_ID, type=ActionType.TASK, title="x", status=ActionStatus.OPEN,
        )
        s.add(ai)
        await s.flush()
        assert await remember_svc.counterparty_address(s, ai) == (None, None)


async def test_remember_about_person_appends_a_dated_line_and_journals(db):
    async with db() as s:
        ai = await _item_with_thread(s)
        await people_svc.set_notes(
            s, await people_svc.ensure_person(s, THEM, "Пётр"), "CTO Ромашки"
        )
        msg = await remember_svc.remember(
            s, item=ai, target="person", text="  технику   отдаю Борису ", addr=THEM, name="Пётр",
            now=NOW,
        )
        await s.commit()
        person = await people_svc.get_person(s, THEM)
        notes = await people_svc.notes_of(s, person)
        decisions = (
            await s.execute(select(AgentDecision).where(AgentDecision.action_item_id == ai.id))
        ).scalars().all()
    assert msg == "Записано о Пётр."
    assert notes == "CTO Ромашки\n2026-09-05: технику отдаю Борису"
    assert person.updated_at == NOW
    # The journal names the profile as well as the address: since docs/feature-people.md a
    # note lives on a person, and that person may later be merged or renamed.
    assert decisions[0].payload == {
        "action": "remember", "text": "технику отдаю Борису", "kind": "person", "addr": THEM,
        "person_id": str(person.id),
    }


async def test_remember_into_profile_appends_a_bullet(db):
    async with db() as s:
        ai = await _item_with_thread(s)
        msg = await remember_svc.remember(
            s, item=ai, target="profile", text="счета от Ромашки — в бухгалтерию",
            addr=THEM, name="Пётр", now=NOW,
        )
        assert msg == "Записано в профиль пространства."
        profile = await profiles.get_profile(s, SPACE_ID)
        assert profile == "- 2026-09-05: счета от Ромашки — в бухгалтерию"
        # Editing the profile text leaves what was remembered in place: those lines are
        # facts of their own now, forgotten one at a time, not by retyping the text.
        await profiles.set_profile(s, SPACE_ID, "Роль: PM")
        await remember_svc.remember(
            s, item=ai, target="profile", text="ещё одно", addr=None, name=None, now=NOW,
        )
        assert await profiles.get_profile(s, SPACE_ID) == (
            "Роль: PM\n- 2026-09-05: счета от Ромашки — в бухгалтерию\n- 2026-09-05: ещё одно"
        )


async def test_person_target_without_an_address_lands_in_the_profile(db):
    async with db() as s:
        ai = await _item_with_thread(s)
        msg = await remember_svc.remember(
            s, item=ai, target="person", text="x", addr=None, name=None, now=NOW,
        )
        assert msg == "Записано в профиль пространства."
        assert await people_svc.get_person(s, THEM) is None


async def test_empty_note_is_rejected(db):
    async with db() as s:
        ai = await _item_with_thread(s)
        with pytest.raises(ValueError):
            await remember_svc.remember(
                s, item=ai, target="person", text="   ", addr=THEM, name=None, now=NOW,
            )


def test_remember_block_offers_the_person_and_confirms():
    class Item:
        id = uuid.UUID(int=7)

    tpl = templates.env.get_template("_remember.html")
    html = tpl.render(item=Item(), remember_addr=THEM, remember_name="Пётр", remembered=None)
    assert '<option value="person">О Пётр</option>' in html
    assert '<option value="profile">' in html
    assert 'hx-post="/item/00000000-0000-0000-0000-000000000007/remember"' in html
    assert "banner" not in html
    html = tpl.render(item=Item(), remember_addr=None, remember_name=None,
                      remembered="Записано в профиль пространства.")
    assert 'value="person"' not in html
    assert '<div class="banner ok">Записано в профиль пространства.</div>' in html
    assert 'id="remember-00000000-0000-0000-0000-000000000007" open>' in html


async def test_remember_about_a_person_a_meeting_only_named(db):
    """Карточка со встречи: письма нет, адреса нет, есть имя из расшифровки. Раньше такая
    заметка молча уходила в профиль пространства (docs/feature-people.md §5)."""
    async with db() as s:
        await mk_space(s)
        ai = ActionItem(space_id=SPACE_ID, type=ActionType.TASK, title="смета", status="open",
                        counterparty="Иванов", source_kind="meeting")
        s.add(ai)
        await s.flush()
        addr, name = await remember_svc.counterparty_target(s, ai)
        assert (addr, name) == ("", "Иванов")

        msg = await remember_svc.remember(
            s, item=ai, target="person", text="ведёт поставки", addr=addr or None, name=name,
            now=NOW,
        )
        await s.commit()
        person = await people_svc.get_person_by_name(s, "Иванов")
        notes = await people_svc.notes_of(s, person) if person else None
    assert msg == "Записано о Иванов."
    assert person is not None and person.email is None
    assert notes == "2026-09-05: ведёт поставки"
