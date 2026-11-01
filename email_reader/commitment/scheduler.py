"""Time-driven transitions: overdue marking + due follow-ups. Run on a schedule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.clock import today as _today
from email_reader.enums import AGING_TYPES, ActionStatus, ActionType, CloseHint, EventType
from email_reader.models import ActionItem, Email
from email_reader.queue import enqueue


async def mark_overdue(session: AsyncSession) -> int:
    from email_reader import advice as advice_svc

    today = _today()
    overdue = (
        ActionItem.due_date.isnot(None),
        ActionItem.due_date < today,
        ActionItem.status.in_([ActionStatus.OPEN, ActionStatus.IN_PROGRESS]),
        # A meeting date is not a missed deadline: past prep items are auto-DONE by
        # the calendar gate, never OVERDUE (which would pollute «Сегодня»).
        ActionItem.type != ActionType.MEETING_PREP,
    )
    # Сначала выбираем — после UPDATE эти карточки уже не найти по OPEN, а просроченная
    # карточка говорит другое, чем та же карточка вчера: совет ей нужен новый.
    becoming = list((await session.execute(select(ActionItem).where(*overdue))).scalars().all())
    result = await session.execute(
        update(ActionItem).where(*overdue).values(status=ActionStatus.OVERDUE)
    )
    for ai in becoming:
        await advice_svc.request(session, ai)
    return result.rowcount or 0


async def enqueue_due_followups(session: AsyncSession) -> int:
    today = _today()
    rows = (
        await session.execute(
            select(ActionItem).where(
                ActionItem.next_check.isnot(None),
                ActionItem.next_check <= today,
                ActionItem.status.in_(
                    [ActionStatus.OPEN, ActionStatus.IN_PROGRESS, ActionStatus.OVERDUE]
                ),
            )
        )
    ).scalars().all()
    for ai in rows:
        await enqueue(session, EventType.FOLLOWUP_DUE, {"action_item_id": str(ai.id)})
        ai.next_check = None  # fired; user can re-arm via snooze
    return len(rows)


async def tick(session: AsyncSession) -> tuple[int, int]:
    from email_reader import app_prefs

    overdue = await mark_overdue(session)
    followups = await enqueue_due_followups(session)
    await flag_idle(session, await app_prefs.get_idle_days(session))
    return overdue, followups


async def flag_idle(session: AsyncSession, days: int) -> int:
    """Пометить затухшие карточки предложением закрыть.

    Движение — правка карточки (`updated_at`: смена типа, срок, «оставить», переразбор)
    или письмо в её беседе-источнике, в любую сторону: у «жду ответа» ответ собеседника и
    есть движение, у обещания — моё письмо. У карточек со встреч и из Telegram беседы нет,
    у них движение только правка. `next_check` остаётся напоминанием (его гасит
    `enqueue_due_followups`), второго счётчика нет (docs/ux-tasks.md §17.4,
    docs/feature-topics.md §8.1). Стареют типы `AGING_TYPES`; «нужен ответ» — никогда.

    Снятие предложения («Оставить») трогает строку, поэтому `updated_at` обновляется и
    отсчёт начинается заново."""
    if days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=days)
    fresh_mail = exists(
        select(Email.id).where(
            Email.thread_id == ActionItem.source_thread_id, Email.sent_at >= cutoff
        )
    )
    result = await session.execute(
        update(ActionItem)
        .where(
            ActionItem.type.in_([str(t) for t in AGING_TYPES]),
            ActionItem.status.in_(
                [ActionStatus.OPEN, ActionStatus.IN_PROGRESS, ActionStatus.OVERDUE]
            ),
            ActionItem.close_candidate.is_(False),
            ActionItem.updated_at < cutoff,
            ~fresh_mail,
        )
        .values(close_candidate=True, close_hint=str(CloseHint.IDLE))
    )
    return result.rowcount or 0
