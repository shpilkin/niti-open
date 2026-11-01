"""Notifications to the user (Telegram). FOLLOWUP_DUE handler + overdue alerts.

Notifications are a low-stakes, ALLOW-policy action. Sending uses the Telegram HTTP
API directly to avoid holding an aiogram session in the worker.
"""

from __future__ import annotations

import logging
import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import history
from email_reader.config import get_settings
from email_reader.enums import AGING_TYPES, CloseHint
from email_reader.models import AgentDecision
from email_reader.views import get_item

log = logging.getLogger("email_reader.notify")


async def send_telegram(text: str) -> None:
    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_allowed_chat_id:
        log.info("Telegram not configured; skipping notification: %s", text)
        return
    url = f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(
                url,
                json={
                    "chat_id": s.telegram_allowed_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("Failed to send Telegram notification")


async def meeting_routed(title: str, projects: list[str], suggested: int, items: int) -> None:
    """Tell the user where a recording landed.

    Niti sends this, not the recorder app: routing happens later and in another process,
    so at the moment the transcript is delivered the projects aren't known yet — and the
    place to correct them is Niti's UI anyway."""
    where = ", ".join(projects) if projects else "без проекта"
    lines = [f"🎙 <b>{title}</b>", f"Проекты: {where}"]
    if suggested:
        lines.append(f"Ждут подтверждения: {suggested}")
    if items:
        lines.append(f"Задач и решений: {items}")
    await send_telegram("\n".join(lines))


async def process_followup_due(session: AsyncSession, payload: dict) -> None:
    """Подошла дата возврата к карточке.

    Раньше отсюда уходило только сообщение в Telegram — и, если бот не настроен, напоминание
    исчезало в логе. Теперь оно ещё и видно в
    интерфейсе: карточка встаёт в секцию «Предлагаю закрыть» своей доски (docs/ux-tasks.md
    §17.4)."""
    ai = await get_item(session, uuid.UUID(payload["action_item_id"]))
    if ai is None:
        return
    if str(ai.type) in {str(t) for t in AGING_TYPES} and not ai.close_candidate:
        ai.close_candidate = True
        ai.close_hint = str(CloseHint.IDLE)
        session.add(AgentDecision(
            kind=history.SUGGEST_CLOSE, action_item_id=ai.id,
            payload={"hint": str(CloseHint.IDLE), "source": "followup"},
        ))
    who = ai.counterparty or "—"
    await send_telegram(
        f"⏰ <b>Напоминание</b>\n{ai.title}\nКонтрагент: {who}\nТип: {ai.type}"
    )
