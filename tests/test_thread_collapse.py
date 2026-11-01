"""Option A: one live obligation per (thread, counterparty).

The card's TYPE is mutable — a single obligation evolves in place as the conversation
progresses (promise → deliver → wait → reply), instead of the same thread spawning a
second card on another board. Threads with several counterparties keep one card each.
"""

from __future__ import annotations

from datetime import UTC, datetime

from email_reader import app_prefs, identities, profiles, prompts
from email_reader.ai.extractor import Usage
from email_reader.ai.schemas import ExtractedItem, ExtractionResult
from email_reader.commitment import engine
from email_reader.enums import ActionStatus, ActionType, Direction
from email_reader.model_connections import ResolvedModel
from email_reader.models import Account, ActionItem, Email, Thread
from tests.conftest import SPACE_ID, mk_space

ME = {"me@ya.ru"}


def _it(type_, cp, title="t"):
    return ExtractedItem(type=type_, title=title, counterparty=cp)


# --- pure helpers -----------------------------------------------------------------


def test_cp_key_normalizes():
    assert engine._cp_key("  Таня ") == "таня"
    assert engine._cp_key("") is None
    assert engine._cp_key(None) is None


def test_state_rank_follows_the_ball():
    inb, out = Direction.INBOUND, Direction.OUTBOUND
    # They wrote last -> my-court types rank above their-court ones.
    assert engine._state_rank(ActionType.NEED_MY_REPLY, inb) < engine._state_rank(
        ActionType.WAITING_FOR_REPLY, inb
    )
    # I wrote last -> their-court types rank above mine.
    assert engine._state_rank(ActionType.WAITING_FOR_REPLY, out) < engine._state_rank(
        ActionType.MY_COMMITMENT, out
    )


def _ai(type_, cp="tanya@x.ru", status=ActionStatus.OPEN, created=1, edited=False):
    return ActionItem(space_id=SPACE_ID, 
        type=str(type_), title="t", counterparty=cp, status=status, user_edited=edited,
        created_at=datetime(2026, 7, created, tzinfo=UTC),
    )


def test_pick_existing_reuses_target_type_row_to_avoid_collision():
    # A resolved WAITING row already owns (thread, WAITING, cp); transitioning the live
    # MY_COMMITMENT row to WAITING would violate the unique constraint — so the resolved
    # WAITING row must be the one picked (and later reopened) instead.
    done_wait = _ai(ActionType.WAITING_FOR_REPLY, status=ActionStatus.DONE, created=1)
    live_commit = _ai(ActionType.MY_COMMITMENT, status=ActionStatus.OPEN, created=2)
    item = ExtractedItem(type=ActionType.WAITING_FOR_REPLY, title="жду")
    assert engine._pick_existing([done_wait, live_commit], item) is done_wait


def test_pick_existing_transitions_live_row_when_no_target_type():
    live_commit = _ai(ActionType.MY_COMMITMENT, created=2)
    item = ExtractedItem(type=ActionType.WAITING_FOR_REPLY, title="жду")
    assert engine._pick_existing([live_commit], item) is live_commit


def test_pick_existing_skips_user_edited():
    edited = _ai(ActionType.MY_COMMITMENT, edited=True)
    item = ExtractedItem(type=ActionType.WAITING_FOR_REPLY, title="жду")
    assert engine._pick_existing([edited], item) is None


def _email(direction, frm, to, from_name=None):
    return Email(
        direction=direction, from_addr=frm, from_name=from_name,
        to_addrs=[{"email": to}], cc_addrs=[],
    )


def test_sole_counterparty_pins_the_one_external():
    emails = [
        _email("inbound", "petya@x.ru", "me@ya.ru", from_name="Пётр"),
        _email("outbound", "me@ya.ru", "petya@x.ru"),
    ]
    assert engine._sole_counterparty(emails, ME) == "Пётр (petya@x.ru)"


def test_sole_counterparty_none_when_several_externals():
    emails = [
        _email("inbound", "petya@x.ru", "me@ya.ru"),
        _email("inbound", "tanya@x.ru", "me@ya.ru"),
    ]
    assert engine._sole_counterparty(emails, ME) is None


def test_sole_counterparty_none_for_self_only_thread():
    emails = [_email("self", "me@ya.ru", "me@ya.ru")]
    assert engine._sole_counterparty(emails, ME) is None


# --- integration: process_thread_ready --------------------------------------------


def _stub_model(monkeypatch, items: list[ExtractedItem]):
    async def fake_extract(*a, **k):
        return ExtractionResult(items=items), Usage(1, 1)

    async def fake_resolve(*a, **k):
        return ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")

    monkeypatch.setattr(engine, "extract", fake_extract)
    monkeypatch.setattr(engine, "resolve_for_thread", fake_resolve)
    monkeypatch.setattr(identities, "get_identity_set", lambda s: _async(ME))
    monkeypatch.setattr(app_prefs, "get_extract_backfill_days", lambda s: _async(0))
    monkeypatch.setattr(profiles, "get_profile", lambda s, a: _async(""))
    monkeypatch.setattr(prompts, "get_commitment_prompt", lambda s, a: _async("sys"))


async def _async(v):
    return v


async def _seed_thread(s, emails: list[tuple[str, str, str]]):
    """emails: (direction, from_addr, to_addr) in chronological order."""
    acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@ya.ru", folders=[])
    s.add(acc)
    await s.flush()
    th = Thread(account_id=acc.id, extract_version=1, extracted_version=0)
    s.add(th)
    await s.flush()
    for i, (direction, frm, to) in enumerate(emails):
        s.add(Email(
            account_id=acc.id, thread_id=th.id, direction=direction, from_addr=frm,
            to_addrs=[{"email": to}], cc_addrs=[], dedup_hash=f"h{i}",
            sent_at=datetime(2026, 7, 1 + i, tzinfo=UTC), subject="s", body_text="b",
        ))
    await s.flush()
    return acc, th


async def _live(s, thread_id):
    from sqlalchemy import select
    rows = (await s.execute(select(ActionItem).where(
        ActionItem.source_thread_id == thread_id,
        ActionItem.status.in_(["open", "in_progress", "overdue"]),
    ))).scalars().all()
    return list(rows)


async def test_commitment_transitions_to_waiting_no_double_card(db, monkeypatch):
    # I promised, then delivered (last message outbound). Even though the LLM DRIFTS and
    # still reports MY_COMMITMENT alongside the new WAITING, the thread must end with ONE
    # live card, of type WAITING, reusing the same physical row (no card on two boards).
    async with db() as s:
        await mk_space(s)
        _, th = await _seed_thread(s, [
            ("outbound", "me@ya.ru", "tanya@x.ru"),  # promise
            ("outbound", "me@ya.ru", "tanya@x.ru"),  # delivery
        ])
        commit = _ai(ActionType.MY_COMMITMENT, cp="tanya@x.ru")
        commit.source_thread_id = th.id
        s.add(commit)
        await s.commit()
        commit_id = commit.id

        _stub_model(monkeypatch, [
            _it(ActionType.MY_COMMITMENT, "tanya@x.ru", "обещание"),
            _it(ActionType.WAITING_FOR_REPLY, "tanya@x.ru", "жду ответ"),
        ])
        await engine.process_thread_ready(s, {"thread_id": str(th.id)})
        await s.commit()

        live = await _live(s, th.id)
        assert len(live) == 1
        assert live[0].id == commit_id  # same row, transitioned in place
        assert live[0].type == ActionType.WAITING_FOR_REPLY
        assert live[0].next_check is not None  # follow-up date set on entering waiting


async def test_two_counterparties_keep_separate_cards(db, monkeypatch):
    # One thread, two people: I owe Tanya a deliverable and I'm waiting on Petya. Both are
    # real, distinct obligations — collapsing is per-counterparty, so BOTH survive.
    async with db() as s:
        await mk_space(s)
        _, th = await _seed_thread(s, [
            ("inbound", "petya@x.ru", "me@ya.ru"),  # me in To -> all types allowed
            ("outbound", "me@ya.ru", "tanya@x.ru"),  # last outbound: no FSM auto-resolve
        ])
        await s.commit()
        _stub_model(monkeypatch, [
            _it(ActionType.MY_COMMITMENT, "tanya@x.ru", "спека Тане"),
            _it(ActionType.WAITING_FOR_REPLY, "petya@x.ru", "жду Петю"),
        ])
        await engine.process_thread_ready(s, {"thread_id": str(th.id)})
        await s.commit()

        live = await _live(s, th.id)
        by_cp = {i.counterparty: i.type for i in live}
        assert by_cp == {
            "tanya@x.ru": ActionType.MY_COMMITMENT,
            "petya@x.ru": ActionType.WAITING_FOR_REPLY,
        }


async def test_empty_counterparty_collapses_into_pinned_card(db, monkeypatch):
    # The bug: an existing card the LLM stored with an EMPTY counterparty, plus a
    # re-extraction that this time NAMES the (same) person. In a 1-on-1 thread both belong
    # to that one participant, so the thread must end with ONE card reusing the same row —
    # not two cards split by the empty-vs-named counterparty.
    async with db() as s:
        await mk_space(s)
        _, th = await _seed_thread(s, [
            ("inbound", "petya@x.ru", "me@ya.ru"),   # he asked
            ("outbound", "me@ya.ru", "petya@x.ru"),  # I replied (last outbound: no FSM close)
        ])
        need = _ai(ActionType.NEED_MY_REPLY, cp="")  # LLM left it empty last time
        need.source_thread_id = th.id
        s.add(need)
        await s.commit()
        need_id = need.id

        _stub_model(monkeypatch, [_it(ActionType.WAITING_FOR_REPLY, "Пётр Петров", "жду")])
        await engine.process_thread_ready(s, {"thread_id": str(th.id)})
        await s.commit()

        live = await _live(s, th.id)
        assert len(live) == 1
        assert live[0].id == need_id  # same physical row, not a fresh second card
        assert live[0].type == ActionType.WAITING_FOR_REPLY
        assert live[0].counterparty == "petya@x.ru"  # pinned to the sole participant


async def test_user_edited_card_is_never_transitioned(db, monkeypatch):
    async with db() as s:
        await mk_space(s)
        _, th = await _seed_thread(s, [
            ("outbound", "me@ya.ru", "tanya@x.ru"),
            ("outbound", "me@ya.ru", "tanya@x.ru"),
        ])
        edited = _ai(ActionType.MY_COMMITMENT, cp="tanya@x.ru", edited=True)
        edited.source_thread_id = th.id
        s.add(edited)
        await s.commit()
        _stub_model(monkeypatch, [_it(ActionType.WAITING_FOR_REPLY, "tanya@x.ru", "жду")])
        await engine.process_thread_ready(s, {"thread_id": str(th.id)})
        await s.commit()

        live = await _live(s, th.id)
        # Human's card stays untouched; automation did not spawn a competing WAITING card.
        assert len(live) == 1
        assert live[0].id == edited.id
        assert live[0].type == ActionType.MY_COMMITMENT
