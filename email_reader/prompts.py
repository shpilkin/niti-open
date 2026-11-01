"""User-editable, per-space system prompts.

Each space can add its own wishes to the built-in system prompts used when its mail and
meetings are processed. They are stored in `app_settings` under `prompt:<key>:<space_id>`
and appended to the built-in default from `ai/prompt.py` — never replace it: the default
carries the contract (types, fields, the tool), and a one-line replacement once left every
meeting of a space without it (26.09.2026). A prompt whose default is empty (mail summary)
is off until filled, so there the owner's text is the whole prompt. Scoped by space to keep
spaces isolated (docs/spaces.md §1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import settings_store
from email_reader.ai.prompt import (
    MAIL_SUMMARY_SUGGESTION,
    MAIL_SUMMARY_SYSTEM,
    MEETING_SYSTEM_PROMPT,
    PROJECT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)


@dataclass(frozen=True)
class PromptSpec:
    key: str            # short id, used in URLs and storage keys
    title: str
    default: str
    description: str     # where/how it's applied — shown in settings to guide edits
    # Ready-to-paste text for prompts whose default is empty (= feature off). Shown as a
    # placeholder; an empty box has to stay empty until the user decides otherwise.
    suggestion: str = ""


PROMPTS: list[PromptSpec] = [
    PromptSpec(
        key="commitment",
        title="Извлечение обязательств",
        default=SYSTEM_PROMPT,
        description=(
            "Системный промпт для разбора каждого треда на обязательства и задачи "
            "(типы MY_COMMITMENT, DELEGATED_TASK, WAITING_FOR_REPLY, NEED_MY_REPLY, INFO). "
            "Применяется при обработке новой почты этого ящика — результат виден в списках "
            "«Сегодня», «Нужен ответ», «Жду ответа», «Мои обещания». Ваш текст "
            "дописывается к встроенному промпту — типы и формат ответа он не меняет."
        ),
    ),
    PromptSpec(
        key="project",
        title="Классификация проектов",
        default=PROJECT_SYSTEM_PROMPT,
        description=(
            "Системный промпт, который решает, к каким проектам этого ящика относится тред. "
            "Применяется при авто-раскладке писем по проектам и при пере-классификации "
            "после изменения описания проекта. Ваш текст "
            "дописывается к встроенному промпту."
        ),
    ),
    PromptSpec(
        key="mail_summary",
        title="Конспект переписки",
        default=MAIL_SUMMARY_SYSTEM,
        suggestion=MAIL_SUMMARY_SUGGESTION,
        description=(
            "Системный промпт для конспекта треда, который кладётся в папку проекта. "
            "ПУСТО (так по умолчанию) — письма копируются в «Переписка/» как есть, "
            "модель не вызывается. Если заполнить, в папке треда вместо отдельных писем "
            "появится один файл «Сводка.md»; вложения копируются в обоих случаях. "
            "Конспект делается на весь тред, один вызов модели на тред, и обновляется "
            "только когда в треде появилось новое письмо."
        ),
    ),
    PromptSpec(
        key="meeting",
        title="Разбор встреч",
        default=MEETING_SYSTEM_PROMPT,
        description=(
            "Системный промпт для разбора расшифрованной встречи: саммари, задачи, решения "
            "и принадлежность проектам — за один вызов. Применяется к записям, которые "
            "передаёт диктофон; результат виден в разделе «Встречи» и в папках проектов. "
            "Ваш текст дописывается к встроенному промпту — типы и формат ответа он не меняет."
        ),
    ),
]

_BY_KEY = {p.key: p for p in PROMPTS}


def spec(key: str) -> PromptSpec:
    return _BY_KEY[key]


def _store_key(key: str, space_id: uuid.UUID) -> str:
    return f"prompt:{key}:{space_id}"


async def get_override(
    session: AsyncSession, key: str, space_id: uuid.UUID
) -> str | None:
    """The saved override text for this space, or None if it uses the default."""
    return await settings_store.get_value(session, _store_key(key, space_id))


ADDITION_HEADER = (
    "\n\nДОПОЛНИТЕЛЬНО ОТ ВЛАДЕЛЬЦА (уточняет разбор; формат ответа, типы и инструмент "
    "остаются прежними):\n"
)


async def get_prompt(
    session: AsyncSession, key: str, space_id: uuid.UUID | None
) -> str:
    """Effective prompt: the built-in default plus the space's additions, if any."""
    default = _BY_KEY[key].default
    if space_id is not None:
        extra = await settings_store.get_value(session, _store_key(key, space_id))
        if extra and extra.strip():
            if not default.strip():
                return extra
            return default + ADDITION_HEADER + extra.strip()
    return default


async def get_commitment_prompt(
    session: AsyncSession, space_id: uuid.UUID | None
) -> str:
    return await get_prompt(session, "commitment", space_id)


async def get_project_prompt(
    session: AsyncSession, space_id: uuid.UUID | None
) -> str:
    return await get_prompt(session, "project", space_id)


async def get_mail_summary_prompt(
    session: AsyncSession, space_id: uuid.UUID | None
) -> str:
    """Empty string = summarisation is off for this space (the default)."""
    return (await get_prompt(session, "mail_summary", space_id)).strip()


async def get_meeting_prompt(
    session: AsyncSession, space_id: uuid.UUID | None
) -> str:
    return await get_prompt(session, "meeting", space_id)


async def set_prompt(
    session: AsyncSession, key: str, space_id: uuid.UUID, value: str
) -> None:
    await settings_store.set_value(session, _store_key(key, space_id), value)


async def reset_prompt(
    session: AsyncSession, key: str, space_id: uuid.UUID
) -> None:
    await settings_store.delete_value(session, _store_key(key, space_id))
