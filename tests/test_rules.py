"""Learned rules: proposed from what the user did, confirmed by the user, then deterministic."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

import email_reader.api.app  # noqa: F401  — registers icon/age/local on the template env
from email_reader import app_prefs, identities, labels, profiles, prompts
from email_reader.ai.extractor import Usage
from email_reader.ai.schemas import ExtractedItem, ExtractionResult
from email_reader.api.common import templates
from email_reader.commitment import engine
from email_reader.enums import ActionStatus, ActionType, Direction, EventType, UserVerdict
from email_reader.memory import rules as rules_svc
from email_reader.memory.directory import PersonRow
from email_reader.memory.rules import Proposal
from email_reader.model_connections import ResolvedModel
from email_reader.models import (
    Account,
    ActionItem,
    AgentDecision,
    Email,
    Event,
    Identity,
    SenderRule,
    Thread,
)
from email_reader.pipeline.process import process_email_stored
from tests.conftest import SPACE_ID, mk_space

ME = "me@b.ru"
THEM = "petrov@romashka.ru"
NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


# ---- matching and creation ------------------------------------------------------------


def test_matching_label_ids_skips_non_label_rules():
    lid = uuid.uuid4()
    rules = [
        SenderRule(match_type="address", pattern=THEM, label_id=lid, action="label"),
        SenderRule(match_type="address", pattern=THEM, action="ignore"),
    ]
    assert labels.matching_label_ids(rules, {THEM}) == {lid}


def test_delegate_rule_prefers_the_address_over_the_domain():
    rules = [
        SenderRule(match_type="domain", pattern="romashka.ru", action="delegate", value="Отдел"),
        SenderRule(match_type="address", pattern=THEM, action="delegate", value="Борис"),
        SenderRule(match_type="address", pattern=THEM, action="ignore"),
    ]
    assert labels.delegate_rule(rules, THEM).value == "Борис"
    assert labels.delegate_rule(rules, "other@romashka.ru").value == "Отдел"
    assert labels.delegate_rule(rules, "x@y.ru") is None


async def test_create_rule_validates_and_dedupes(db):
    async with db() as s:
        assert await labels.create_rule(s, "address", THEM, None, action="label") is None
        assert await labels.create_rule(s, "address", THEM, None, action="delegate") is None
        r = await labels.create_rule(s, "address", THEM, None, action="delegate", value=" Борис ")
        assert r is not None and r.value == "Борис" and r.label_id is None
        again = await labels.create_rule(s, "address", THEM, None, action="delegate", value="Борис")
        assert again is None
        assert await labels.create_rule(s, "address", THEM, None, action="bogus") is None
        ig = await labels.create_rule(s, "domain", "@Romashka.RU", None, action="ignore")
        assert ig is not None and ig.pattern == "romashka.ru"
        assert [r.action for r in await labels.rules_for_address(s, THEM)] == ["delegate", "ignore"]


async def test_ignored_by_rule_is_inbound_only(db):
    async with db() as s:
        await labels.create_rule(s, "domain", "romashka.ru", None, action="ignore")
        inbound = Email(direction=Direction.INBOUND, from_addr=THEM)
        mine = Email(direction=Direction.OUTBOUND, from_addr=ME, to_addrs=[{"email": THEM}])
        assert await labels.ignored_by_rule(s, inbound)
        assert not await labels.ignored_by_rule(s, mine)


# ---- application ------------------------------------------------------------------------


async def test_ignored_sender_gets_no_extraction(db):
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="acc", host="h", port=993, login=ME)
        s.add(acc)
        s.add(Identity(email=ME))
        await s.flush()
        await labels.create_rule(s, "address", THEM, None, action="ignore")
        e = Email(
            account_id=acc.id, dedup_hash=uuid.uuid4().hex, direction=Direction.INBOUND,
            from_addr=THEM, to_addrs=[{"email": ME}], cc_addrs=[], sent_at=NOW,
            subject="Счёт", body_text="Оплатите", headers={},
        )
        s.add(e)
        await s.commit()
        await process_email_stored(s, {"email_id": str(e.id)})
        await s.commit()
        assert e.is_low_value is True
        ready = await s.execute(select(Event).where(Event.type == EventType.THREAD_READY))
        assert ready.scalars().all() == []


async def _async(v):
    return v


async def test_delegate_rule_routes_the_request_after_extraction(db, monkeypatch):
    async def fake_extract(messages, today, **kw):
        return ExtractionResult(items=[
            ExtractedItem(type=ActionType.NEED_MY_REPLY, title="Ответить по срокам",
                          counterparty=THEM, reason="просит сроки"),
        ]), Usage(1, 1)

    async def fake_resolve(*a, **k):
        return ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")

    monkeypatch.setattr(engine, "extract", fake_extract)
    monkeypatch.setattr(engine, "resolve_for_thread", fake_resolve)
    monkeypatch.setattr(identities, "get_identity_set", lambda s: _async({ME}))
    monkeypatch.setattr(app_prefs, "get_extract_backfill_days", lambda s: _async(0))
    monkeypatch.setattr(profiles, "get_profile", lambda s, a: _async(""))
    monkeypatch.setattr(prompts, "get_commitment_prompt", lambda s, a: _async("sys"))

    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="acc", host="h", port=993, login=ME)
        s.add(acc)
        await s.flush()
        await labels.create_rule(s, "address", THEM, None, action="delegate", value="Борис")
        th = Thread(account_id=acc.id, extract_version=1, extracted_version=0)
        s.add(th)
        await s.flush()
        s.add(Email(
            account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
            direction=Direction.INBOUND, from_addr=THEM, to_addrs=[{"email": ME}],
            cc_addrs=[], sent_at=NOW - timedelta(hours=1), subject="Сроки",
        ))
        await s.commit()
        await engine.process_thread_ready(s, {"thread_id": str(th.id)})
        await s.commit()
        items = (await s.execute(select(ActionItem))).scalars().all()
        decision = (
            await s.execute(select(AgentDecision).where(AgentDecision.kind == "extract"))
        ).scalar_one()
    assert [(i.type, i.delegate_to) for i in items] == [(ActionType.TO_DELEGATE, "Борис")]
    assert items[0].reason.startswith("По правилу для отправителя — Борис.")
    assert decision.payload["delegate_rule"] == "Борис"


# ---- proposals ----------------------------------------------------------------------------


def _card(type_, status, **more):
    return {"type": type_, "status": status, **more}


async def _sender_cards(s, statuses_and_more):
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name="acc", host="h", port=993, login=ME)
    s.add(acc)
    await s.flush()
    for spec in statuses_and_more:
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()
        s.add(Email(
            account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
            direction=Direction.INBOUND, from_addr=THEM, to_addrs=[{"email": ME}],
            cc_addrs=[], sent_at=NOW - timedelta(days=len(statuses_and_more)),
        ))
        s.add(ActionItem(space_id=SPACE_ID, title="x", source_thread_id=th.id, **spec))
    await s.commit()


async def test_three_dismissals_propose_ignore(db):
    async with db() as s:
        await _sender_cards(s, [
            _card(ActionType.NEED_MY_REPLY, ActionStatus.DISMISSED, resolved_by="user"),
            _card(ActionType.NEED_MY_REPLY, ActionStatus.DISMISSED, resolved_by="user"),
            _card(ActionType.INFO, ActionStatus.DISMISSED, resolved_by="user"),
            _card(ActionType.NEED_MY_REPLY, ActionStatus.DISMISSED, resolved_by="auto"),
        ])
        props = await rules_svc.propose_for(s, SPACE_ID, THEM)
        assert [(p.action, p.pattern) for p in props] == [("ignore", THEM)]
        assert props[0].evidence.startswith("3 из последних 4 карточек")
        # Confirmed -> the proposal is gone, the rule is there.
        await rules_svc.accept(s, match_type="address", pattern=THEM, action="ignore", value=None)
        assert await rules_svc.propose_for(s, SPACE_ID, THEM) == []


async def test_a_kept_card_blocks_the_ignore_proposal(db):
    async with db() as s:
        await _sender_cards(s, [
            _card(ActionType.NEED_MY_REPLY, ActionStatus.DISMISSED, resolved_by="user"),
            _card(ActionType.NEED_MY_REPLY, ActionStatus.DISMISSED, resolved_by="user"),
            _card(ActionType.NEED_MY_REPLY, ActionStatus.DISMISSED, resolved_by="user"),
            _card(ActionType.NEED_MY_REPLY, ActionStatus.DONE, user_verdict=UserVerdict.CONFIRMED),
        ])
        assert await rules_svc.propose_for(s, SPACE_ID, THEM) == []


async def test_two_handoffs_propose_delegate(db):
    async with db() as s:
        await _sender_cards(s, [
            _card(ActionType.TO_DELEGATE, ActionStatus.DONE, delegate_to="Борис"),
            _card(ActionType.TO_DELEGATE, ActionStatus.OPEN, delegate_to="Борис"),
            _card(ActionType.TO_DELEGATE, ActionStatus.DISMISSED, delegate_to="Оля"),
        ])
        props = await rules_svc.propose_for(s, SPACE_ID, THEM)
        assert [(p.action, p.value) for p in props] == [("delegate", "Борис")]
        assert props[0].text == "делегировать: Борис"


async def test_other_space_cards_do_not_count(db):
    async with db() as s:
        await _sender_cards(
            s, [_card(ActionType.NEED_MY_REPLY, ActionStatus.DISMISSED, resolved_by="user")] * 3
        )
        assert await rules_svc.propose_for(s, uuid.UUID(int=2), THEM) == []


# ---- the block --------------------------------------------------------------------------


def test_memory_block_lists_rules_and_proposals():
    rule = SenderRule(id=uuid.UUID(int=9), match_type="domain", pattern="romashka.ru",
                      action="delegate", value="Борис")
    prop = Proposal(
        "ignore", None, "address", THEM, "3 из последних 4 карточек — «не обязательство»"
    )
    row = PersonRow(key=f"email:{THEM}", name="Пётр", identities=[("email", THEM, "Пётр")])
    html = templates.env.get_template("_personmemory.html").render(
        row=row, email=THEM, name="Пётр", person=None, stats=None, facts=[], space="abc",
        rules=[(rule, "делегировать: Борис")], proposals=[prop],
    )
    assert "домен <code>romashka.ru</code> → делегировать: Борис" in html
    assert 'hx-post="/person/rules/00000000-0000-0000-0000-000000000009/delete"' in html
    assert "Сделать правилом — не разбирать письма?" in html
    assert '<input type="hidden" name="action" value="ignore">' in html
    assert "кто угодно с домена romashka.ru" in html
