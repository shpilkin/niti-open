"""Local storage maintenance: manual pruning of old mail from the LOCAL store only.

This NEVER touches the IMAP server — it deletes rows from the local SQLite DB and
the on-disk attachment cache. The unit of deletion is a whole thread whose last
activity is older than the cutoff (keeps referential integrity: a thread's emails,
action items, labels and project memberships are removed together). Threads with any
recent activity are kept in full.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dateutil.relativedelta import relativedelta
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.config import get_settings
from email_reader.models import (
    ActionItem,
    AgentDecision,
    Email,
    EmailLabel,
    EmailOccurrence,
    EvalSample,
    ProjectThread,
    Thread,
)
from email_reader.paths import get_data_dir

# VACUUM copies the database next to itself; keep a margin on top of that copy.
_VACUUM_HEADROOM = 256 * 1024 * 1024


def cutoff_for(amount: int, unit: str) -> datetime:
    """Compute the 'older than' cutoff (UTC). unit is 'days' or 'months'."""
    now = datetime.now(UTC)
    if unit == "months":
        return now - relativedelta(months=amount)
    return now - timedelta(days=amount)


def _email_ids_query(before: datetime):
    """Email ids eligible for pruning: in an old thread, or orphaned and old."""
    old_threads = select(Thread.id).where(Thread.last_at.is_not(None), Thread.last_at < before)
    return select(Email.id).where(
        or_(
            Email.thread_id.in_(old_threads),
            (Email.thread_id.is_(None)) & (Email.sent_at < before),
        )
    )


def _old_threads_query(before: datetime):
    return select(Thread.id).where(Thread.last_at.is_not(None), Thread.last_at < before)


async def llm_usage(session: AsyncSession) -> list[dict]:
    """LLM spend from the AgentDecision trace: calls + tokens by kind, 7 and 30 days.

    Tokens are recorded by extraction/classification (memo/draft calls go through the
    same trace as they gain usage reporting). Shown in Settings → AI so gateway costs
    (RouterAI etc.) are visible instead of a surprise on the invoice."""
    from email_reader.models import AgentDecision

    out: list[dict] = []
    now = datetime.now(UTC)
    for label, days in (("7 дней", 7), ("30 дней", 30)):
        row = (
            await session.execute(
                select(
                    func.count(AgentDecision.id),
                    func.coalesce(func.sum(AgentDecision.tokens_in), 0),
                    func.coalesce(func.sum(AgentDecision.tokens_out), 0),
                ).where(
                    AgentDecision.created_at >= now - timedelta(days=days),
                    AgentDecision.tokens_in.isnot(None),
                )
            )
        ).one()
        out.append(
            {"period": label, "calls": row[0], "tokens_in": row[1], "tokens_out": row[2]}
        )
    return out


def db_path() -> Path | None:
    """The SQLite file behind the app, or None when the DB is not SQLite."""
    url = get_settings().database_url
    if not url.startswith("sqlite"):
        return None
    _, _, tail = url.partition(":///")
    return Path(tail) if tail else None


async def db_size(session: AsyncSession) -> dict:
    """File size and how much of it is free pages (reclaimable by a VACUUM).

    A deleted or rebuilt index frees pages inside the file; SQLite reuses them but
    never shrinks the file on its own, and a `.backup` copies free pages too.
    """
    path = db_path()
    if path is None:
        return {"bytes": 0, "free_bytes": 0, "wal_bytes": 0}
    on_disk = path.stat().st_size if path.exists() else 0
    wal = path.with_name(path.name + "-wal")
    page = (await session.execute(text("PRAGMA page_size"))).scalar_one()
    free = (await session.execute(text("PRAGMA freelist_count"))).scalar_one()
    return {
        "bytes": on_disk,
        "free_bytes": int(page) * int(free),
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
    }


async def compact() -> dict:
    """VACUUM: rewrite the file without its free pages. Returns before/after sizes.

    VACUUM builds a full copy next to the original, so it needs free disk space of
    roughly the DB size and must not run inside a transaction — hence a dedicated
    autocommit connection instead of the request's session.
    """
    from email_reader.db import get_engine

    path = db_path()
    if path is None:
        return {"skipped": "not_sqlite", "before": 0, "after": 0, "freed": 0}
    before = path.stat().st_size if path.exists() else 0
    free_disk = shutil.disk_usage(path.parent).free
    if free_disk < before + _VACUUM_HEADROOM:
        return {
            "skipped": "no_disk_space", "before": before, "after": before, "freed": 0,
            "need": before + _VACUUM_HEADROOM, "free_disk": free_disk,
        }
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("VACUUM"))
    after = path.stat().st_size if path.exists() else 0
    return {"skipped": None, "before": before, "after": after, "freed": max(0, before - after)}


async def local_stats(session: AsyncSession) -> dict:
    total_emails = (await session.execute(select(func.count(Email.id)))).scalar_one()
    total_threads = (await session.execute(select(func.count(Thread.id)))).scalar_one()
    oldest = (await session.execute(select(func.min(Email.sent_at)))).scalar_one()
    newest = (await session.execute(select(func.max(Email.sent_at)))).scalar_one()
    return {
        "total_emails": total_emails,
        "total_threads": total_threads,
        "oldest": oldest,
        "newest": newest,
        "size": await db_size(session),
    }


async def preview_prune(session: AsyncSession, before: datetime) -> dict:
    """Count what a prune at `before` would remove. No changes are made."""
    eid_q = _email_ids_query(before).subquery()
    tid_q = _old_threads_query(before).subquery()
    n_emails = (
        await session.execute(select(func.count()).select_from(eid_q))
    ).scalar_one()
    n_threads = (
        await session.execute(select(func.count()).select_from(tid_q))
    ).scalar_one()
    n_items = (
        await session.execute(
            select(func.count(ActionItem.id)).where(
                ActionItem.source_thread_id.in_(_old_threads_query(before))
            )
        )
    ).scalar_one()
    return {"emails": n_emails, "threads": n_threads, "action_items": n_items, "before": before}


def _rmtree_caches(email_ids: list[str]) -> None:
    base = get_data_dir() / "attachments"
    for eid in email_ids:
        shutil.rmtree(base / str(eid), ignore_errors=True)


async def prune_local_before(session: AsyncSession, before: datetime) -> dict:
    """Delete local data for threads/emails older than `before`. Server is untouched.

    Returns counts of what was removed.
    """
    # Capture the concrete email ids first — needed to clear the on-disk attachment
    # cache (named by email id) after the rows are gone.
    email_ids = [str(e) for e in (await session.execute(_email_ids_query(before))).scalars()]
    if not email_ids:
        return {"emails": 0, "threads": 0, "action_items": 0}

    eid_q = _email_ids_query(before)
    tid_q = _old_threads_query(before)

    # Count before deleting (for the result message).
    n_threads = (
        await session.execute(select(func.count()).select_from(tid_q.subquery()))
    ).scalar_one()
    n_items = (
        await session.execute(
            select(func.count(ActionItem.id)).where(ActionItem.source_thread_id.in_(tid_q))
        )
    ).scalar_one()

    # FK-safe deletion order (SQLite enforces foreign_keys=ON). Subqueries avoid
    # binding huge id lists as parameters.
    # 1. Preserve the audit log: detach decisions from action items we're removing.
    await session.execute(
        update(AgentDecision)
        .where(
            AgentDecision.action_item_id.in_(
                select(ActionItem.id).where(ActionItem.source_thread_id.in_(tid_q))
            )
        )
        .values(action_item_id=None)
    )
    # 2. Action items of old threads, and any lingering message refs.
    await session.execute(delete(ActionItem).where(ActionItem.source_thread_id.in_(tid_q)))
    await session.execute(
        update(ActionItem)
        .where(ActionItem.source_message_id.in_(eid_q))
        .values(source_message_id=None)
    )
    # 3. Eval samples keep their snapshot but lose the thread ref.
    await session.execute(
        update(EvalSample).where(EvalSample.thread_id.in_(tid_q)).values(thread_id=None)
    )
    # 4. Provenance, memberships, labels, then emails, then threads.
    await session.execute(delete(EmailOccurrence).where(EmailOccurrence.email_id.in_(eid_q)))
    await session.execute(delete(ProjectThread).where(ProjectThread.thread_id.in_(tid_q)))
    await session.execute(delete(EmailLabel).where(EmailLabel.email_id.in_(eid_q)))
    await session.execute(delete(Email).where(Email.id.in_(eid_q)))
    await session.execute(delete(Thread).where(Thread.id.in_(tid_q)))

    # 5. On-disk attachment cache (filesystem, outside the transaction).
    await asyncio.to_thread(_rmtree_caches, email_ids)

    return {"emails": len(email_ids), "threads": n_threads, "action_items": n_items}
