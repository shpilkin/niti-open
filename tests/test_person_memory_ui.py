"""The «Память» block of the person pane renders from the same facts the assistant will
cite, and the pane loads it lazily so the dossier route itself stays untouched.

Since docs/feature-people.md the block hangs on a profile, not on an address: a person met
only at a meeting is remembered too. The address still gates the parts that are computed
from mail — the statistics and the sender rules.
"""

from __future__ import annotations

from datetime import UTC, datetime

import email_reader.api.app  # noqa: F401  — registers icon/age/local on the template env
from email_reader.api.common import templates
from email_reader.memory.directory import PersonRow
from email_reader.memory.stats import PersonStats, describe

ADDR = "petrov@romashka.ru"


def _stats():
    return PersonStats(
        email=ADDR, inbound=2, inbound_to_me=1, replied=1, median_reply_hours=2.0,
        total_inbound=2, first_seen_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _row(*, addr=ADDR, name="Пётр", person_id=None):
    idents = [("email", addr, name)] if addr else [("name", (name or "").lower(), name)]
    return PersonRow(
        key=f"person:{person_id}" if person_id else f"email:{addr}" if addr else "name:пётр",
        name=name or addr, person_id=person_id, identities=idents,
    )


def test_memory_block_offers_to_add_notes_when_none():
    st = _stats()
    row = _row()
    html = templates.env.get_template("_personmemory.html").render(
        row=row, email=ADDR, name="Пётр", person=None, stats=st, facts=describe(st), space="",
    )
    assert f'name="key" value="{row.key}"' in html
    assert "Пишет мне: 2 за 90 дней" in html
    assert "Добавить, что я знаю" in html
    assert 'hx-post="/person/notes"' in html


def test_memory_block_shows_existing_notes_open():
    class P:
        updated_at = datetime(2026, 9, 4, 12, tzinfo=UTC)

    st = _stats()
    html = templates.env.get_template("_personmemory.html").render(
        row=_row(), email=ADDR, name=None, person=P(), stats=st, facts=describe(st),
        notes="CTO Ромашки, технику — Борису", space="abc",
    )
    assert '<details class="edit" open>' in html
    assert ">CTO Ромашки, технику — Борису</textarea>" in html
    assert "04.09.2026" in html
    assert 'name="space" value="abc"' in html


def test_memory_block_keeps_notes_for_a_person_without_an_address():
    """Was: «Адрес не определён — память привязана к e-mail», and no form at all. A person
    who only ever came up at a meeting could not be remembered; now the notes are the same
    and only the mail-derived half is missing."""
    html = templates.env.get_template("_personmemory.html").render(
        row=_row(addr=None, name="Пётр"), email=None, name="Пётр", person=None, stats=None,
        facts=[], space="",
    )
    assert 'hx-post="/person/notes"' in html
    assert "Добавить, что я знаю" in html
    assert "Почта к профилю не привязана" in html
    assert "Правило для этого отправителя" not in html


def test_person_pane_loads_the_memory_block_lazily():
    html = templates.env.get_template("_personpane.html").render(
        row=_row(name="Пётр \'Петров\'"), q="Пётр", items=[], emails=[], meetings=[],
        back_item="abc", space="",
    )
    assert 'hx-get="/person/memory"' in html
    assert 'hx-trigger="load"' in html
    assert '"item": "abc"' in html
    assert "\'" not in html.split("hx-vals=\'", 1)[1].split("\'", 1)[0]  # JSON is attribute-safe


# --- The remembered lines under the profile editor (settings → Профиль) ----------------


def test_remembered_lines_render_with_their_date_and_a_forget_button():
    """Rows are `capture.Proposal`s since the memory screen: a «Запомнить» line has no
    source, a confirmed find names its kind and links the letter it came from."""
    from email_reader.memory.capture import Proposal

    class Fact:
        id = "0f0f0f0f-0000-0000-0000-000000000001"
        text = "счета шлю в бухгалтерию"
        valid_from = datetime(2026, 9, 5, 12, tzinfo=UTC)

    class Found(Fact):
        id = "0f0f0f0f-0000-0000-0000-000000000002"
        text = "Борис отвечает за бэкенд"

    class Space:
        id = "abc"

    rows = [
        Proposal(fact=Fact(), kind_label="факт", about="", url="", source_label=""),
        Proposal(fact=Found(), kind_label="зона ответственности", about="",
                 url="/email/0f0f0f0f-0000-0000-0000-00000000aaaa",
                 source_label="письмо от 12.09.2026"),
    ]
    html = templates.env.get_template("settings/_remembered.html").render(
        space=Space(), remembered=rows,
    )
    assert 'id="remembered-abc"' in html
    assert "05.09.2026 — счета шлю в бухгалтерию" in html
    assert 'hx-post="/settings/profile/abc/forget"' in html
    assert 'hx-target="#memory-abc"' in html
    assert 'name="fact_id" value="0f0f0f0f-0000-0000-0000-000000000001"' in html
    assert html.count(">Забыть<") == 2
    # the find: its kind and where it came from, opening in the pane
    assert "(зона ответственности;" in html
    assert 'hx-get="/email/0f0f0f0f-0000-0000-0000-00000000aaaa" hx-target="#pane"' in html
    assert ">письмо от 12.09.2026<" in html
    assert html.count("hx-get=") == 1  # a «Запомнить» line has no source to open


def test_no_remembered_lines_means_an_empty_block():
    class Space:
        id = "abc"

    html = templates.env.get_template("settings/_remembered.html").render(
        space=Space(), remembered=[],
    )
    assert 'id="remembered-abc"' in html and "Забыть" not in html


async def test_forget_route_retires_the_line_and_answers_with_the_block(db, monkeypatch):
    """POST /settings/profile/{space}/forget: the line is gone from the block and from the
    prompt text, but the row stays (retired) — nothing is deleted."""
    from contextlib import asynccontextmanager

    from sqlalchemy import select
    from starlette.requests import Request

    from email_reader import profiles
    from email_reader.api.routers import settings as settings_router
    from email_reader.models import MemoryFact
    from tests.conftest import mk_space

    @asynccontextmanager
    async def scope():
        async with db() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    monkeypatch.setattr(settings_router, "session_scope", scope)
    async with db() as s:
        sp = await mk_space(s)
        await profiles.set_profile(s, sp.id, "Роль: PM.")
        keep = await profiles.remember_line(
            s, sp.id, "первое", now=datetime(2026, 9, 5, tzinfo=UTC)
        )
        gone = await profiles.remember_line(
            s, sp.id, "второе", now=datetime(2026, 9, 6, tzinfo=UTC)
        )
        await s.commit()

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                       "query_string": b""})
    response = await settings_router.profile_forget(request, sp.id, fact_id=str(gone.id))
    html = response.body.decode()
    assert "05.09.2026 — первое" in html and "второе" not in html
    assert f'value="{keep.id}"' in html

    async with db() as s:
        assert await profiles.get_profile(s, sp.id) == "Роль: PM.\n- 2026-09-05: первое"
        rows = (await s.execute(select(MemoryFact))).scalars().all()
        assert len(rows) == 3 and (await s.get(MemoryFact, gone.id)).valid_to is not None

    # A bad id is not an error — the block just comes back as it is.
    response = await settings_router.profile_forget(request, sp.id, fact_id="not-an-id")
    assert "первое" in response.body.decode()
