"""Delegation: per-mailbox profile + TO_DELEGATE items with a suggested team.

The profile (role + teams) is appended to the extraction prompt; the model returns
TO_DELEGATE items with delegate_to (a team from the profile), which drive the
«Делегировать» board.
"""

from __future__ import annotations

from email_reader import profiles, views
from email_reader.ai.prompt import profile_block
from email_reader.ai.schemas import TOOL_SCHEMA, ExtractedItem
from email_reader.enums import ActionType
from email_reader.models import Account, ActionItem, Thread
from tests.conftest import SPACE_ID, mk_space


def test_profile_block_empty_and_filled():
    assert profile_block("") == ""
    assert profile_block(None) == ""
    assert profile_block("   ") == ""
    b = profile_block("Команды:\n- Аналитика — метрики")
    assert "О ВЛАДЕЛЬЦЕ И КОМАНДАХ" in b
    assert "Аналитика" in b


def test_schema_exposes_delegate_to_and_new_type():
    props = TOOL_SCHEMA["input_schema"]["properties"]["items"]["items"]["properties"]
    assert "delegate_to" in props
    assert "TO_DELEGATE" in props["type"]["enum"]
    # empty string -> None (like counterparty)
    it = ExtractedItem(type=ActionType.TO_DELEGATE, title="x", delegate_to="", confidence=0.5)
    assert it.delegate_to is None
    it2 = ExtractedItem(type=ActionType.TO_DELEGATE, title="x", delegate_to="Разработка")
    assert it2.delegate_to == "Разработка"


async def test_profile_roundtrip_and_trim(db):
    async with db() as s:
        await mk_space(s)
        a = Account(space_id=SPACE_ID, name="work", host="imap.x", login="me@x")
        s.add(a)
        await s.flush()
        assert await profiles.get_profile(s, a.space_id) == ""  # none yet
        await profiles.set_profile(s, a.space_id, "  Роль: PM  ")
        await s.commit()
        assert await profiles.get_profile(s, a.space_id) == "Роль: PM"  # trimmed on save


async def test_upsert_stores_delegate_to(db):
    from email_reader.commitment import engine

    async with db() as s:

        await mk_space(s)
        t = Thread()
        s.add(t)
        await s.flush()
        item = ExtractedItem(
            type=ActionType.TO_DELEGATE, title="Оценить ТЗ по закупке",
            delegate_to="Разработка", confidence=0.8, reason="запрос на оценку",
        )
        ai = await engine._upsert_item(s, t.id, None, item, followup_days=3, existing=None,
                                       space_id=SPACE_ID)
        await s.commit()
        assert ai.type == ActionType.TO_DELEGATE
        assert ai.delegate_to == "Разработка"


async def test_delegated_board_shows_both_their_court_types(db):
    """«Делегировано» is the home of everything in the other side's court: work still to
    hand off (TO_DELEGATE, first section) AND work already handed off (DELEGATED_TASK,
    second section). Before, an undated DELEGATED_TASK was on no board at all."""
    async with db() as s:
        await mk_space(s)
        s.add(ActionItem(
            space_id=SPACE_ID, type=ActionType.DELEGATED_TASK, title="already delegated",
            status="open"))
        s.add(ActionItem(
            space_id=SPACE_ID, type=ActionType.TO_DELEGATE, title="hand off", status="open"))
        await s.commit()
        b = await views.board(s, "delegated")
        assert [(r.item.title, r.section) for r in b.rows] == [
            ("hand off", "Передать команде"),
            ("already delegated", "Ждут исполнения"),
        ]
        assert b.sections == [("Передать команде", 1), ("Ждут исполнения", 1)]
