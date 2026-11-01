"""Threading: assign each email to a Thread.

Primary signal: RFC headers (Message-ID / In-Reply-To / References) — union by
reference. Fallback: normalized-subject match against a recent thread sharing a
participant. Merges threads when a new email links previously separate ones.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.models import ActionItem, Email, Thread
from email_reader.normalization import normalize_subject

SUBJECT_FALLBACK_DAYS = 30


async def _threads_by_references(session: AsyncSession, email: Email) -> set:
    ref_ids = set(email.reference_ids or [])
    if email.in_reply_to:
        ref_ids.add(email.in_reply_to)

    found: set = set()

    # All lookups are scoped to the email's own account: cross-account conversations
    # stay separate threads (strict mailbox isolation).
    acc = email.account_id

    # Threads of emails THIS email references (parent already stored).
    if ref_ids:
        rows = await session.execute(
            select(Email.thread_id).where(
                Email.account_id == acc,
                Email.rfc_message_id.in_(ref_ids),
                Email.thread_id.isnot(None),
            )
        )
        found |= {tid for (tid,) in rows if tid is not None}

    # Threads of emails that reference THIS email (child already stored, parent late).
    if email.rfc_message_id:
        mid = email.rfc_message_id
        rows = await session.execute(
            select(Email.thread_id).where(
                Email.account_id == acc,
                Email.thread_id.isnot(None),
                or_(
                    Email.in_reply_to == mid,
                    Email.reference_ids.contains([mid]),
                ),
            )
        )
        found |= {tid for (tid,) in rows if tid is not None}

    return found


async def _thread_by_subject(session: AsyncSession, email: Email) -> object | None:
    subj = normalize_subject(email.subject)
    if not subj:
        return None
    cutoff = email.sent_at - timedelta(days=SUBJECT_FALLBACK_DAYS)
    candidates = (
        await session.execute(
            select(Thread)
            .where(
                Thread.account_id == email.account_id,  # same-mailbox threads only
                Thread.subject_norm == subj,
                Thread.last_at >= cutoff,
            )
            .order_by(Thread.last_at.desc())
            .limit(5)
        )
    ).scalars().all()

    participants = {email.from_addr} | {
        a.get("email") for a in (email.to_addrs + email.cc_addrs) if a.get("email")
    }
    for thread in candidates:
        rows = await session.execute(
            select(Email.from_addr).where(Email.thread_id == thread.id)
        )
        thread_people = {addr for (addr,) in rows}
        if thread_people & participants:
            return thread
    return None


async def _merge_threads(session: AsyncSession, keep_id, drop_ids: set) -> None:
    drop_ids = {d for d in drop_ids if d != keep_id}
    if not drop_ids:
        return
    # Action items of dropped threads are regenerated on the next extraction of the
    # merged thread, so we drop them here to avoid FK/unique conflicts (rare path).
    await session.execute(
        delete(ActionItem).where(ActionItem.source_thread_id.in_(drop_ids))
    )
    await session.execute(
        update(Email).where(Email.thread_id.in_(drop_ids)).values(thread_id=keep_id)
    )
    await session.execute(delete(Thread).where(Thread.id.in_(drop_ids)))


async def recompute_thread(session: AsyncSession, thread: Thread) -> None:
    agg = (
        await session.execute(
            select(
                func.min(Email.sent_at),
                func.max(Email.sent_at),
                func.count(Email.id),
            ).where(Email.thread_id == thread.id)
        )
    ).one()
    thread.first_at, thread.last_at, thread.message_count = agg


async def rethread(session: AsyncSession, email: Email) -> bool:
    """Уже разложенное письмо, у которого появились Message-ID или ссылки цепочки, — в
    цепочку, с которой его связывают ссылки. True — письмо переехало.

    Цепочки не сливаются: слияние удаляет карточки слитой цепочки (`_merge_threads`), а у
    лежащих цепочек они уже могли быть в работе. Карточки из самого письма едут с ним."""
    old_id = email.thread_id
    if old_id is None:
        return False
    related = await _threads_by_references(session, email)
    if not related or old_id in related:
        return False
    target_id = min(related, key=str)
    items = (
        await session.execute(
            select(ActionItem).where(
                ActionItem.source_message_id == email.id,
                ActionItem.source_thread_id == old_id,
            )
        )
    ).scalars().all()
    for item in items:
        # Одна карточка на (цепочка, тип, собеседник): такая же в новой цепочке — остаётся.
        taken = item.counterparty is not None and await session.scalar(
            select(ActionItem.id).where(
                ActionItem.source_thread_id == target_id,
                ActionItem.type == item.type,
                ActionItem.counterparty == item.counterparty,
            ).limit(1)
        )
        if not taken:
            item.source_thread_id = target_id
    email.thread_id = target_id
    await session.flush()
    for thread_id in (old_id, target_id):
        thread = await session.get(Thread, thread_id)
        if thread is not None:
            await recompute_thread(session, thread)
    (await session.get(Thread, target_id)).extract_version += 1
    return True


async def assign_thread(session: AsyncSession, email: Email) -> Thread:
    """Attach `email` to a thread (existing/merged/new) and refresh aggregates."""
    if email.thread_id is not None:
        # Повторный разбор лежащего письма (текст или ссылки пришли позже): оно остаётся на
        # месте, пока ссылки не ведут в другую цепочку, — без слияний и без поиска по теме.
        await rethread(session, email)
        thread = await session.get(Thread, email.thread_id)
        if thread is not None:
            await recompute_thread(session, thread)
            thread.extract_version += 1
            return thread
    related = await _threads_by_references(session, email)

    if related:
        keep_id = sorted(str(t) for t in related)[0]
        keep_id = next(t for t in related if str(t) == keep_id)
        await _merge_threads(session, keep_id, related)
        thread = (
            await session.execute(select(Thread).where(Thread.id == keep_id))
        ).scalar_one()
    else:
        thread = await _thread_by_subject(session, email)
        if thread is None:
            thread = Thread(
                subject_norm=normalize_subject(email.subject),
                account_id=email.account_id,
            )
            session.add(thread)
            await session.flush()

    email.thread_id = thread.id
    await session.flush()
    await recompute_thread(session, thread)
    thread.extract_version += 1
    return thread
