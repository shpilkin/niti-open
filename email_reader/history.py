"""История карточки: журнал решений, прочитанный по-человечески.

Карточка обязательства живёт долго и меняется молча: ассистент переразбирает тред и
меняет тип на месте, конечный автомат закрывает её, когда мяч перешёл, планировщик
помечает просроченной. Всё это уже пишется в `agent_decisions`, но до сих пор никем не
читалось (docs/ux-tasks.md §17).

Здесь одно правило: **снятие — не закрытие**. Большинство автоматических закрытий
— это «модель перестала возвращать пункт», а не «обязательство исполнено».
Выдавать одно за другое нельзя, иначе история врёт.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.enums import TYPE_LABEL
from email_reader.models import AgentDecision

# Что показываем: только записи, привязанные к карточке. `extract` и `project_match`
# привязаны к треду и говорят о проходе разбора, а не о судьбе этой карточки.
KINDS = ("intake", "transition", "auto_resolve", "suggest_close", "user_action")

TRANSITION = "transition"
SUGGEST_CLOSE = "suggest_close"
REASON_MAX = 160

# Причины автозакрытия -> (текст, снято ли это вместо закрытия).
_AUTO_REASON: dict[str, tuple[str, bool]] = {
    "counterparty replied": ("собеседник ответил", False),
    "I replied": ("я ответил", False),
    "delegated / replied": ("передано или отвечено", False),
    "superseded after thread update": ("ассистент перестал находить это в треде", True),
    "meeting_deleted": ("встреча удалена", True),
}

_CLOSE_HINT = {
    "acknowledged": "собеседник отозвался после моего обещания",
    "idle": "давно ничего не происходит",
}

_USER_ACTION = {
    "complete": "Готово, вручную",
    "dismiss": "Не обязательство",
    "undo": "Отмена закрытия",
    "reopen": "Снова открыта, вручную",
    "confirm": "На доску",
    "keep_open": "Оставлено открытым, я решил не закрывать",
    "remember": "Записано в память о человеке",
    "create": "Создана",
    "create_from_email": "Создана из письма",
}

# Откуда пришло действие (views.ORIGIN). Действие внешнего агента в истории не должно
# выглядеть как клик человека за экраном.
_VIA = {"mcp": "через MCP"}


@dataclass(frozen=True)
class Entry:
    at: datetime
    text: str
    # Кто это сделал: "user" — я, "auto" — ассистент, "drop" — карточку сняли, не закрыли.
    actor: str


def _label(type_: str | None) -> str:
    key = str(type_ or "")
    return TYPE_LABEL.get(key, key or "без типа")


def _clip(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= REASON_MAX else text[: REASON_MAX - 1] + "…"


def _date(value: str | None) -> str:
    """ISO-дату из журнала — в тот же вид, что на карточке."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value


def _auto_resolve_text(reason: str | None) -> tuple[str, str]:
    text, dropped = _AUTO_REASON.get(
        (reason or "").strip(), (_clip(reason) or "без причины", True)
    )
    if dropped:
        return f"Снята с доски: {text}", "drop"
    return f"Закрыта: {text}", "auto"


def describe(kind: str, payload: dict) -> tuple[str, str] | None:
    """(текст, actor) для одной записи журнала, либо None — показывать нечего."""
    payload = payload or {}
    if kind == TRANSITION:
        if payload.get("action") == "reopen":
            return "Снова открыта: в треде новое сообщение", "auto"
        head = f"{_label(payload.get('from'))} → {_label(payload.get('to'))}"
        reason = _clip(payload.get("reason"))
        return (f"{head}: {reason}" if reason else head), "auto"
    if kind == "auto_resolve":
        return _auto_resolve_text(payload.get("reason"))
    if kind == SUGGEST_CLOSE:
        why = _CLOSE_HINT.get(payload.get("hint") or "", "")
        return ("Предложено закрыть" + (f": {why}" if why else "")), "auto"
    if kind == "intake":
        return "Пришла из Telegram, ждёт разбора", "auto"
    if kind != "user_action":
        return None

    action = payload.get("action") or ""
    text = _user_action_text(action, payload)
    if text is None:
        return None
    via = _via_text(str(payload.get("via") or ""))
    return (f"{text} ({via})" if via else text), "user"


def _via_text(via: str) -> str | None:
    """«через MCP» и, когда рукопожатие назвало клиента, — «через MCP, Claude Code»."""
    if not via.startswith("mcp"):
        return _VIA.get(via)
    _, _, client = via.partition("/")
    return f"через MCP, {client}" if client else _VIA["mcp"]


def _user_action_text(action: str, payload: dict) -> str | None:
    if action in ("retype", "retype_merge"):
        to = _label(payload.get("to"))
        return (
            f"Тип изменён вручную: {to}"
            + (" (слито с существующей карточкой)" if action == "retype_merge" else "")
        )
    if action == "snooze":
        return f"Отложено до {_date(payload.get('until'))}".strip()
    if action == "set_due":
        due = _date(payload.get("due"))
        return f"Срок: {due}" if due else "Срок снят"
    if action == "duplicate":
        of = _clip(payload.get("of_title"))
        return f"Закрыта как дубль карточки «{of}»" if of else "Закрыта как дубль"
    if action == "delegate":
        to = _clip(payload.get("to"))
        return f"Передано: {to}" if to else "Передано"
    if action == "keep_topic":
        return "Оставлена: Нить продолжается, отсчёт тишины заново"
    if action == "dismiss" and payload.get("reason") == "idle_topic":
        return "Снята с досок вместе с затухшей Нитью"
    return _USER_ACTION.get(action)


async def for_item(session: AsyncSession, item_id: uuid.UUID) -> list[Entry]:
    """Историю карточки от старого к новому — читается как рассказ, не как лог."""
    rows = (
        await session.execute(
            select(AgentDecision)
            .where(AgentDecision.action_item_id == item_id, AgentDecision.kind.in_(KINDS))
            .order_by(AgentDecision.created_at.asc(), AgentDecision.id.asc())
        )
    ).scalars().all()
    out: list[Entry] = []
    for row in rows:
        said = describe(row.kind, row.payload)
        if said is None:
            continue
        text, actor = said
        out.append(Entry(at=row.created_at, text=text, actor=actor))
    return out
