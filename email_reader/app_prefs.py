"""Runtime-editable app preferences.

Overrides for a few `config.yaml` defaults, stored in `app_settings` so they can be
changed from the Settings UI without editing files or restarting. When no override is
set, the built-in config default is used.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import settings_store
from email_reader.config import get_app_config

EXTRACT_BACKFILL_KEY = "extract_backfill_days"
IDLE_DAYS_KEY = "idle_days"
IDLE_DAYS_DEFAULT = 30
REMIND_DAYS_KEY = "remind_days"
REMIND_DAYS_DEFAULT = 14


async def get_extract_backfill_days(session: AsyncSession) -> int:
    """Only extract commitments from threads whose latest message is within this many
    days (0 = no limit). Override wins over the config.yaml default (30 ≈ 1 month)."""
    v = await settings_store.get_value(session, EXTRACT_BACKFILL_KEY)
    if v is not None and v != "":
        try:
            return max(0, int(v))
        except ValueError:
            pass
    return int(getattr(get_app_config(), "extract_backfill_days", 30))


async def set_extract_backfill_days(session: AsyncSession, days: int) -> None:
    await settings_store.set_value(session, EXTRACT_BACKFILL_KEY, str(max(0, int(days))))


async def get_idle_days(session: AsyncSession) -> int:
    """Через сколько дней без движения ассистент предлагает закрыть делегированное или
    обещание (0 = не предлагать никогда). См. docs/ux-tasks.md §17.4."""
    v = await settings_store.get_value(session, IDLE_DAYS_KEY)
    if v is not None and v != "":
        try:
            return max(0, int(v))
        except ValueError:
            pass
    return IDLE_DAYS_DEFAULT


async def set_idle_days(session: AsyncSession, days: int) -> None:
    await settings_store.set_value(session, IDLE_DAYS_KEY, str(max(0, int(days))))


async def get_remind_days(session: AsyncSession) -> int:
    """Через сколько дней без движения по чужому ходу («жду ответа», «передано») строка
    получает пометку «пора напомнить» (0 = не напоминать). Напоминание — владельцу, людям
    ничего не отправляется. См. docs/feature-topics.md §8.1."""
    v = await settings_store.get_value(session, REMIND_DAYS_KEY)
    if v is not None and v != "":
        try:
            return max(0, int(v))
        except ValueError:
            pass
    return REMIND_DAYS_DEFAULT


async def set_remind_days(session: AsyncSession, days: int) -> None:
    await settings_store.set_value(session, REMIND_DAYS_KEY, str(max(0, int(days))))
