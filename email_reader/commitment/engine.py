"""THREAD_READY handler: re-extract a thread and reconcile ActionItems + lifecycle.

Idempotent on (thread_id, extract_version). user_edited items are never overwritten
by automation. Stale items (no longer present after a thread update) are auto-resolved;
deterministic FSM handles WAITING/NEED_REPLY auto-resolution and delegated close-candidates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import history, prompts
from email_reader import labels as labels_svc
from email_reader import spaces as spaces_svc
from email_reader.ai.extractor import extract
from email_reader.ai.schemas import ExtractedItem
from email_reader.clock import today as _today
from email_reader.config import get_app_config
from email_reader.enums import (
    LIVE_STATUSES,
    ActionStatus,
    ActionType,
    CloseHint,
    Direction,
    SourceKind,
)
from email_reader.model_connections import resolve_for_thread
from email_reader.models import ActionItem, AgentDecision, Email, SenderRule, Thread
from email_reader.normalization import normalize_addr
from email_reader.search import index as search_index

_OUTBOUND = {Direction.OUTBOUND, Direction.SELF}

# One obligation per (thread, counterparty): with respect to a single person the ball is
# always in exactly one court. These orderings decide which state is "current" when the LLM
# returns several obligations for the SAME counterparty in one pass — and act as a safety
# net against it asserting two contradictory states for one person. Lower index = more
# current, given the direction of the thread's last message.
_MY_COURT = (ActionType.NEED_MY_REPLY, ActionType.TO_DELEGATE, ActionType.MY_COMMITMENT)
_THEIR_COURT = (ActionType.WAITING_FOR_REPLY, ActionType.DELEGATED_TASK)


def _cp_key(counterparty: str | None) -> str | None:
    return (counterparty or "").strip().lower() or None


def _state_rank(type_: ActionType, last_direction: Direction) -> int:
    # They wrote last -> ball in my court; I wrote last -> ball in theirs.
    if last_direction == Direction.INBOUND:
        order = (*_MY_COURT, *_THEIR_COURT)
    else:
        order = (*_THEIR_COURT, *_MY_COURT)
    try:
        return order.index(type_)
    except ValueError:
        return len(order)


def _pick_existing(group: list[ActionItem], item: ExtractedItem) -> ActionItem | None:
    """Choose which existing (non-user-edited) card of this counterparty to become the
    thread's obligation for `item`. The card's TYPE is mutated in place, so ordering matters
    for the (thread, type, counterparty) uniqueness constraint:

    1. An existing card ALREADY of the target type (any status) — reuse it verbatim; a live
       one, else the latest resolved (reopened). This is the only branch that can face a
       type collision, and it sidesteps it by reusing the very row that owns that type.
    2. Otherwise any live card of another type — safe to transition, since (1) proved no
       row of the target type exists for this counterparty.
    3. Otherwise the latest resolved card — reopened by fresh activity (_upsert_item reopens
       only DONE; a DISMISSED «не обязательство» card is honoured, swallowing the re-asserted
       obligation instead of resurfacing it).

    Extra live duplicates left behind are superseded by reconciliation."""
    usable = sorted(
        (r for r in group if not r.user_edited),
        key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC),
    )
    target = [r for r in usable if r.type == str(item.type)]
    if target:
        live_t = [r for r in target if r.status in LIVE_STATUSES]
        return live_t[0] if live_t else target[-1]
    live = [r for r in usable if r.status in LIVE_STATUSES]
    if live:
        return live[0]
    return usable[-1] if usable else None


def _sole_counterparty(emails: list[Email], identities: set[str]) -> str | None:
    """For a 1-on-1 thread, the single external participant as 'Name (email)' (or bare
    email if no name). None when there are zero or several distinct external addresses —
    then we can't disambiguate, so we don't force a counterparty.

    Used to keep one obligation per (thread, counterparty): the LLM is inconsistent about
    this field across re-extractions (empty one pass, name the next), which would split one
    person's card into two. In a 1-on-1 thread every obligation is with that one person by
    definition, so we override the field with this canonical value."""
    name_by_addr: dict[str, str] = {}
    order: list[str] = []

    def _consider(email: str | None, name: str | None) -> None:
        addr = normalize_addr(email or "")
        if not addr or addr in identities:
            return
        clean = (name or "").strip()
        if addr not in name_by_addr:
            name_by_addr[addr] = clean
            order.append(addr)
        elif not name_by_addr[addr] and clean:
            name_by_addr[addr] = clean

    for e in emails:
        _consider(e.from_addr, e.from_name)
        for a in (e.to_addrs or []) + (e.cc_addrs or []):
            _consider((a or {}).get("email"), (a or {}).get("name"))

    if len(order) != 1:
        return None
    addr = order[0]
    name = name_by_addr[addr]
    return f"{name} ({addr})" if name else addr


def _party(email: str | None, name: str | None) -> str:
    """The stored form of a counterparty: «Name (addr)», or the bare address."""
    addr = normalize_addr(email or "")
    clean = (name or "").strip()
    return f"{clean} ({addr})" if clean else addr


def _fallback_counterparty(
    type_: ActionType, emails: list[Email], identities: set[str]
) -> str | None:
    """The other party read off the thread when the model left the field empty — only
    where the thread itself makes it unambiguous, so a row is never labelled by a guess.

    NEED_MY_REPLY: the sender of the last inbound message (it is their question).
    WAITING_FOR_REPLY / DELEGATED_TASK / MY_COMMITMENT: the single external To-recipient
    of my last message (I asked / instructed / promised them); several -> None.
    Anything else (TO_DELEGATE, INFO, …) has no counterparty by nature."""
    if type_ == ActionType.NEED_MY_REPLY:
        for e in reversed(emails):
            if e.direction != Direction.INBOUND:
                continue
            addr = normalize_addr(e.from_addr or "")
            if addr and addr not in identities:
                return _party(e.from_addr, e.from_name)
            return None
        return None
    if type_ in (
        ActionType.WAITING_FOR_REPLY, ActionType.DELEGATED_TASK, ActionType.MY_COMMITMENT
    ):
        for e in reversed(emails):
            if e.direction not in _OUTBOUND:
                continue
            external: dict[str, str | None] = {}
            for a in e.to_addrs or []:
                addr = normalize_addr((a or {}).get("email") or "")
                if addr and addr not in identities:
                    external.setdefault(addr, (a or {}).get("name"))
            if len(external) != 1:
                return None
            ((addr, name),) = external.items()
            return _party(addr, name)
        return None
    return None


def _thread_flags(emails: list[Email], identities: set[str]) -> tuple[bool, bool]:
    """Deterministic thread facts: (has_outbound, me_in_to).

    has_outbound — the thread contains at least one email I sent.
    me_in_to — I am a direct To-recipient of at least one inbound email (CC does not
    count: watching a thread in copy creates no obligations for me)."""
    has_outbound = any(e.direction in _OUTBOUND for e in emails)
    me_in_to = False
    for e in emails:
        if e.direction != Direction.INBOUND:
            continue
        for a in e.to_addrs or []:
            addr = normalize_addr((a or {}).get("email") or "")
            if addr and addr in identities:
                me_in_to = True
                break
        if me_in_to:
            break
    return has_outbound, me_in_to


async def _delegate_rule_for_thread(
    session: AsyncSession, emails: list[Email], identities: set[str]
) -> SenderRule | None:
    """The confirmed delegate rule for the sender of the thread's last inbound message."""
    last_in = next((e for e in reversed(emails) if e.direction == Direction.INBOUND), None)
    if last_in is None:
        return None
    addr = normalize_addr(last_in.from_addr or "")
    if not addr or addr in identities:
        return None
    rules = (await session.execute(select(SenderRule))).scalars().all()
    return labels_svc.delegate_rule(list(rules), addr)


def _allowed_types(has_outbound: bool, me_in_to: bool) -> set[ActionType]:
    """Per-type deterministic gates, applied to LLM output (the LLM can hallucinate
    obligations for threads where I'm a bystander):

    - NEED_MY_REPLY / TO_DELEGATE: only if I'm in To of an inbound email — a reply or
      hand-off is owed by the addressee, not by someone in copy.
    - MY_COMMITMENT / WAITING_FOR_REPLY / DELEGATED_TASK: only if the thread has my
      outbound email — a promise or request requires me having actually written."""
    allowed: set[ActionType] = set()
    if me_in_to:
        allowed |= {ActionType.NEED_MY_REPLY, ActionType.TO_DELEGATE}
    if has_outbound:
        allowed |= {
            ActionType.MY_COMMITMENT,
            ActionType.WAITING_FOR_REPLY,
            ActionType.DELEGATED_TASK,
        }
    return allowed


async def _thread_emails(session: AsyncSession, thread_id: uuid.UUID) -> list[Email]:
    rows = await session.execute(
        select(Email).where(Email.thread_id == thread_id).order_by(Email.sent_at)
    )
    return list(rows.scalars().all())


def render_messages(emails: list[Email]) -> list[dict]:
    out = []
    for e in emails:
        is_me = e.direction in _OUTBOUND
        who = e.from_name or e.from_addr
        out.append(
            {
                "is_me": is_me,
                "who": who,
                "sent_at": e.sent_at,
                "subject": e.subject,
                "body": e.body_text,
            }
        )
    return out


# The cards saved before the thread fallback existed are named once, at startup.
COUNTERPARTY_BACKFILL_KEY = "counterparty.fallback_v1"
_FALLBACK_TYPES = (
    ActionType.NEED_MY_REPLY, ActionType.WAITING_FOR_REPLY, ActionType.DELEGATED_TASK,
    ActionType.MY_COMMITMENT,
)


async def backfill_counterparties(session: AsyncSession) -> dict[str, int]:
    """One pass over the live mail cards saved with an empty counterparty: name them by
    the thread rule (_fallback_counterparty), in place. Marks itself done, so it runs
    once. User-edited cards are not touched; a name that would collide with an existing
    (thread, type, counterparty) card is skipped — that card already is the obligation."""
    from email_reader import settings_store
    from email_reader.identities import get_identity_set

    if await settings_store.get_value(session, COUNTERPARTY_BACKFILL_KEY):
        return {"scanned": 0, "named": 0}
    rows = (
        await session.execute(
            select(ActionItem).where(
                ActionItem.source_kind == SourceKind.MAIL,
                ActionItem.source_thread_id.is_not(None),
                ActionItem.status.in_([s.value for s in LIVE_STATUSES]),
                ActionItem.user_edited.is_(False),
                or_(ActionItem.counterparty.is_(None), ActionItem.counterparty == ""),
                ActionItem.type.in_([t.value for t in _FALLBACK_TYPES]),
            )
        )
    ).scalars().all()
    identities = await get_identity_set(session)
    emails_of: dict[uuid.UUID, list[Email]] = {}
    named = 0
    for ai in rows:
        tid = ai.source_thread_id
        if tid not in emails_of:
            emails_of[tid] = list((await session.execute(
                select(Email).where(Email.thread_id == tid).order_by(Email.sent_at)
            )).scalars().all())
        cp = _fallback_counterparty(ActionType(ai.type), emails_of[tid], identities)
        if not cp:
            continue
        taken = await session.execute(
            select(ActionItem.id).where(
                ActionItem.source_thread_id == tid, ActionItem.type == ai.type,
                ActionItem.counterparty == cp,
            )
        )
        if taken.first() is not None:
            continue
        ai.counterparty = cp
        named += 1
    await settings_store.set_value(session, COUNTERPARTY_BACKFILL_KEY, "1")
    return {"scanned": len(rows), "named": named}


async def _live_items(session: AsyncSession, thread_id: uuid.UUID) -> list[ActionItem]:
    rows = await session.execute(
        select(ActionItem).where(
            ActionItem.source_thread_id == thread_id,
            ActionItem.status.in_([s.value for s in LIVE_STATUSES]),
        )
    )
    return list(rows.scalars().all())


def _followup_check(item: ExtractedItem, followup_days: int) -> date | None:
    if item.type in (ActionType.WAITING_FOR_REPLY, ActionType.DELEGATED_TASK):
        return _today() + timedelta(days=followup_days)
    return None


async def _upsert_item(
    session: AsyncSession,
    thread_id: uuid.UUID,
    source_message_id: uuid.UUID | None,
    item: ExtractedItem,
    followup_days: int,
    existing: ActionItem | None,
    space_id: uuid.UUID,
) -> ActionItem:
    """Create or transition THE single card for (thread, counterparty). The card's TYPE is
    mutable: one obligation per counterparty evolves in place as the thread progresses,
    instead of spawning a second card on another board (`existing` is chosen by
    _pick_existing; None -> create)."""
    if existing is not None:
        if existing.user_edited:
            return existing  # never overwrite human corrections
        # The card's type changes in place, which used to happen silently. Journal it so
        # the pane can say what changed and why (email_reader/history.py).
        if str(existing.type) != str(item.type):
            session.add(AgentDecision(
                kind=history.TRANSITION, action_item_id=existing.id,
                payload={
                    "from": str(existing.type), "to": str(item.type),
                    "reason": item.reason or "",
                },
            ))
        existing.type = str(item.type)
        existing.title = item.title
        existing.counterparty = item.counterparty
        existing.due_date = item.due_date
        existing.delegate_to = item.delegate_to
        existing.confidence = item.confidence
        existing.reason = item.reason
        existing.source_message_id = source_message_id
        check = _followup_check(item, followup_days)
        if check is None:
            existing.next_check = None  # left the waiting state -> drop stale reaction date
        elif existing.next_check is None:
            existing.next_check = check  # entered a waiting state -> set a follow-up date
        if existing.status == ActionStatus.DONE:
            existing.status = ActionStatus.OPEN  # reopened by fresh activity
            existing.resolved_by = None
            existing.close_candidate = False
            existing.close_hint = None
            session.add(AgentDecision(
                kind=history.TRANSITION, action_item_id=existing.id,
                payload={"action": "reopen"},
            ))
        return existing

    next_check = _followup_check(item, followup_days)

    ai = ActionItem(
        source_kind=SourceKind.MAIL,
        space_id=space_id,
        type=str(item.type),
        title=item.title,
        counterparty=item.counterparty,
        due_date=item.due_date,
        delegate_to=item.delegate_to,
        status=ActionStatus.OPEN,
        next_check=next_check,
        source_thread_id=thread_id,
        source_message_id=source_message_id,
        confidence=item.confidence,
        reason=item.reason,
    )
    session.add(ai)
    await session.flush()
    await search_index.index_card(session, ai)
    return ai


def _suggest_close(session: AsyncSession, ai: ActionItem, hint: str) -> None:
    """Предложить закрытие, не закрывая: карточка уезжает в секцию «Предлагаю закрыть», где
    решает пользователь. Повторно не журналим — предложение одно, пока его не снимут."""
    if ai.close_candidate and ai.close_hint == str(hint):
        return
    ai.close_candidate = True
    ai.close_hint = str(hint)
    session.add(AgentDecision(
        kind=history.SUGGEST_CLOSE, action_item_id=ai.id, payload={"hint": str(hint)},
    ))


def _resolve(session: AsyncSession, ai: ActionItem, reason: str) -> None:
    ai.status = ActionStatus.DONE
    ai.resolved_by = "auto"
    session.add(
        AgentDecision(
            kind="auto_resolve",
            action_item_id=ai.id,
            payload={"reason": reason},
        )
    )


async def process_thread_ready(session: AsyncSession, payload: dict) -> None:
    thread_id = uuid.UUID(payload["thread_id"])
    thread = (
        await session.execute(select(Thread).where(Thread.id == thread_id))
    ).scalar_one_or_none()
    if thread is None:
        return
    if thread.extracted_version >= thread.extract_version:
        return  # already extracted this version (idempotent)

    cfg = get_app_config()
    emails = await _thread_emails(session, thread_id)
    if not emails:
        thread.extracted_version = thread.extract_version
        return

    # Backfill guard: skip the LLM for old threads so the first 90-day sync doesn't
    # fire a call per ancient thread. Mark as extracted so it isn't retried. The depth
    # is user-editable in Settings (falls back to the config.yaml default).
    from email_reader import app_prefs
    backfill = await app_prefs.get_extract_backfill_days(session)
    if backfill:
        from datetime import datetime

        if emails[-1].sent_at < datetime.now(UTC) - timedelta(days=backfill):
            thread.extracted_version = thread.extract_version
            return

    # Deterministic pre-LLM gate: if I never wrote in this thread AND no inbound email
    # addresses me directly (I'm at most in CC), nothing here can be my obligation —
    # e.g. two colleagues talking with me in copy. Skip the LLM entirely (cost + noise)
    # and retire previously-extracted items that violate the rule.
    from email_reader.identities import get_identity_set

    identities = await get_identity_set(session)
    has_outbound, me_in_to = _thread_flags(emails, identities)
    # Canonical counterparty for a 1-on-1 thread (None if not exactly one external party).
    canon_cp = _sole_counterparty(emails, identities)
    if not has_outbound and not me_in_to:
        for stale in await _live_items(session, thread_id):
            if not stale.user_edited:
                _resolve(session, stale, "observer thread: я не адресат и не участник")
        session.add(
            AgentDecision(
                kind="skip_extract",
                payload={"thread_id": str(thread_id), "reason": "observer_thread"},
            )
        )
        thread.extracted_version = thread.extract_version
        return
    allowed_types = _allowed_types(has_outbound, me_in_to)

    space_id = await spaces_svc.space_of_account(session, thread.account_id)
    if space_id is None:
        # The mailbox was deleted meanwhile: an item needs a space, and this thread has
        # none to offer. Recorded, not silent.
        session.add(
            AgentDecision(
                kind="skip_extract", payload={"thread_id": str(thread_id), "reason": "no_space"}
            )
        )
        thread.extracted_version = thread.extract_version
        return
    rm = await resolve_for_thread(session, thread_id)  # model of the thread's space
    system_prompt = await prompts.get_commitment_prompt(session, space_id)
    # Append the space owner's profile (role + teams) so TO_DELEGATE items can be routed
    # to the right team. No-op when the space has no profile. The core only: what was
    # remembered on top of it comes through the memory block, by relevance.
    from email_reader import profiles
    from email_reader.ai.prompt import profile_block

    system_prompt += profile_block(await profiles.core(session, space_id))
    # What the space remembers around this thread (counterparty, projects, earlier threads
    # with them, facts that touch it) — reference data under the same framing as the profile.
    from email_reader.memory import context as memory_ctx

    memory = await memory_ctx.for_thread(
        session, thread_id=thread_id, emails=emails, identities=identities, space_id=space_id,
    )
    system_prompt += memory.render("extract")
    result, usage = await extract(
        render_messages(emails),
        _today(),
        kind=rm.kind,
        base_url=rm.base_url,
        api_key=rm.api_key,
        model=rm.model,
        no_thinking=rm.no_thinking,
        system_prompt=system_prompt,
    )
    # After the model: a write before the call holds SQLite's write lock through all of it.
    from email_reader.memory import stats as stats_svc

    for card in memory.people:
        if card.email:
            await stats_svc.refresh(session, card.email, space_id)

    decision = AgentDecision(
        kind="extract",
        payload={
            "thread_id": str(thread_id),
            "version": thread.extract_version,
            "item_count": len(result.items),
            "memory": memory.audit(),
        },
        model=rm.model,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
    )
    session.add(decision)

    last_message_id = emails[-1].id
    last_direction = emails[-1].direction

    # One obligation per counterparty: if the LLM returns several for the same person in a
    # single pass, keep the one that reflects the current ball (whose-court by last message).
    reps: dict[str | None, ExtractedItem] = {}
    fallback_keys: set[str] = set()  # names read off the thread, not returned by the model
    guard_dropped = 0
    for item in result.items:
        if item.type == ActionType.INFO:
            continue
        # Deterministic type gate: drop LLM output whose preconditions the thread
        # doesn't satisfy (see _allowed_types).
        if item.type not in allowed_types:
            guard_dropped += 1
            continue
        # 1-on-1 thread: pin the counterparty to the sole participant so re-extractions
        # can't split one person's obligation into two cards over an empty/renamed field.
        if canon_cp is not None:
            item.counterparty = canon_cp
        elif _cp_key(item.counterparty) is None:
            # The model left the field empty: read the other party off the thread.
            item.counterparty = _fallback_counterparty(item.type, emails, identities)
            if item.counterparty:
                fallback_keys.add(_cp_key(item.counterparty))
        k = _cp_key(item.counterparty)
        cur = reps.get(k)
        if cur is None or _state_rank(item.type, last_direction) < _state_rank(
            cur.type, last_direction
        ):
            reps[k] = item
    if guard_dropped:
        decision.payload = {**decision.payload, "guard_dropped": guard_dropped}

    # A confirmed sender rule hands this person's requests to a team — deterministic, after
    # the type gate (a hand-off is still owed only by the addressee), before cards are written.
    rule = await _delegate_rule_for_thread(session, emails, identities)
    if rule is not None and ActionType.TO_DELEGATE in allowed_types:
        routed = 0
        for item in reps.values():
            if item.type in (ActionType.NEED_MY_REPLY, ActionType.TO_DELEGATE):
                item.type = ActionType.TO_DELEGATE
                item.delegate_to = rule.value
                item.reason = f"По правилу для отправителя — {rule.value}. " + (item.reason or "")
                routed += 1
        if routed:
            decision.payload = {**decision.payload, "delegate_rule": rule.value}

    # Existing cards for this thread grouped by counterparty (any status: lets a resolved
    # card reopen on fresh activity, and lets us collapse pre-existing duplicates of one
    # person into a single evolving card).
    all_items = (
        await session.execute(
            select(ActionItem).where(ActionItem.source_thread_id == thread_id)
        )
    ).scalars().all()
    by_cp: dict[str | None, list[ActionItem]] = {}
    for ai in all_items:
        # In a 1-on-1 thread all cards belong to the sole participant regardless of how the
        # field was (or wasn't) filled before — group them together so pre-existing cards
        # with an empty counterparty collapse into the pinned one instead of surviving.
        key = _cp_key(canon_cp) if canon_cp is not None else _cp_key(ai.counterparty)
        by_cp.setdefault(key, []).append(ai)

    kept: list[ActionItem] = []
    for k, item in reps.items():
        # A name read off the thread is new to the stored cards: the card it belongs to
        # is the one saved with an empty counterparty — pick it up and name it in place
        # instead of creating a twin (or fighting a user-edited one).
        group = by_cp.get(k) or (by_cp.get(None, []) if k in fallback_keys else [])
        if any(r.user_edited for r in group):
            continue  # human owns this counterparty's card; don't compete with it
        existing = _pick_existing(group, item)
        kept.append(await _upsert_item(
            session, thread_id, last_message_id, item, cfg.followup_days, existing, space_id
        ))

    # Flush first: freshly-created cards get their id only on INSERT, and kept_ids drives
    # which live cards reconciliation keeps vs supersedes.
    await session.flush()
    kept_ids: set[uuid.UUID] = {r.id for r in kept}

    # Reconcile: supersede every live card we didn't keep — this both retires vanished
    # obligations and collapses any pre-existing duplicate cards of a counterparty into the
    # one we transitioned above. Then run the deterministic FSM on the survivors.
    for ai in await _live_items(session, thread_id):
        if ai.user_edited:
            continue
        if ai.id not in kept_ids:
            _resolve(session, ai, "superseded after thread update")
            continue
        # Deterministic FSM on still-present items.
        if ai.type == ActionType.WAITING_FOR_REPLY and last_direction == Direction.INBOUND:
            _resolve(session, ai, "counterparty replied")
        elif ai.type == ActionType.NEED_MY_REPLY and last_direction in _OUTBOUND:
            _resolve(session, ai, "I replied")
        elif ai.type == ActionType.TO_DELEGATE and last_direction in _OUTBOUND:
            _resolve(session, ai, "delegated / replied")
        elif ai.type == ActionType.DELEGATED_TASK and last_direction == Direction.INBOUND:
            _suggest_close(session, ai, CloseHint.ACKNOWLEDGED)
        elif ai.type == ActionType.MY_COMMITMENT and last_direction == Direction.INBOUND:
            # Counterparty responded after my promise — likely I delivered and they
            # acknowledged (their "спасибо"). Flag as a close candidate, don't auto-
            # resolve: an inbound "где файл?" is also last-inbound but still owed.
            _suggest_close(session, ai, CloseHint.ACKNOWLEDGED)

    thread.extracted_version = thread.extract_version

    # Hand off to project classification (handler is a no-op if no projects exist).
    from email_reader.enums import EventType
    from email_reader.queue import enqueue

    await enqueue(session, EventType.PROJECT_MATCH, {"thread_id": str(thread_id)})

    # И совет: письмо в треде меняет и то, что карточка означает, и что с ней делать.
    # Считает его воркер отдельным событием — карточка появится и без него.
    from email_reader import advice as advice_svc

    for ai in await _live_items(session, thread_id):
        await advice_svc.request(session, ai)
