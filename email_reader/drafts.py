"""Assistant-recommended email drafts for actionable items.

NEED_MY_REPLY -> a substantive reply; WAITING_FOR_REPLY -> a follow-up whose tone
escalates with the silence (ping number + days quiet); TO_DELEGATE -> a delegation
message to the team suggested by the mailbox profile (item.delegate_to).

Design rules: generated on demand (a button, never eagerly for every card); lands in
the regular compose form and is NEVER sent automatically — the user is the final gate.
Context fed to the model: the thread tail, the mailbox profile (delegation), the memory
block with its evidence sections — the counterparty, open cards with them and on this
thread, the projects' dossiers (`memory.context`, `cards=True`) — and the user's own
outbound messages as style samples. Reply language follows the thread.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import profiles
from email_reader import spaces as spaces_svc
from email_reader.ai.extractor import complete_text
from email_reader.enums import ActionType, Direction
from email_reader.identities import get_identity_set
from email_reader.memory import context as memory_ctx
from email_reader.models import ActionItem, Email, Thread

log = logging.getLogger("email_reader.drafts")

# Item types that have a draft recommendation (and their button labels).
DRAFTABLE: dict[str, str] = {
    ActionType.NEED_MY_REPLY: "Черновик ответа",
    ActionType.WAITING_FOR_REPLY: "Черновик фолоу-апа",
    ActionType.TO_DELEGATE: "Черновик делегирования",
}

_OUTBOUND = {Direction.OUTBOUND, Direction.SELF}
_THREAD_CHARS = 6000  # tail-biased budget for thread context
_MSG_CHARS = 1500
_STYLE_SAMPLES = 2
_STYLE_CHARS = 600


@dataclass
class DraftContext:
    item: ActionItem
    thread_block: str
    profile: str = ""
    style_block: str = ""
    days_silent: int = 0
    prior_pings: int = 0
    account_id: uuid.UUID | None = None
    memory_block: str = ""  # memory.context.MemoryContext.render("draft")
    # What the caller wants said, in their words («перенеси на неделю»). Empty from the
    # draft button; filled by the MCP tool, where asking for a draft and saying what it
    # should say is one request.
    instructions: str = ""
    # What the assistant's advice said the reply must cover (advice.cached_points); the
    # draft is written to this outline rather than from the thread cold.
    plan: list[str] = field(default_factory=list)


def _render_tail(emails: list[Email]) -> str:
    """Last messages of the thread within the char budget (newest kept, order restored)."""
    parts: list[str] = []
    used = 0
    for e in reversed(emails):
        who = "Я" if e.direction in _OUTBOUND else (e.from_name or e.from_addr)
        text = (e.body_text or "").strip()[:_MSG_CHARS]
        block = f"[{who} — {e.sent_at:%d.%m.%Y}]\n{text}"
        if used + len(block) > _THREAD_CHARS and parts:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(reversed(parts))


def followup_stats(emails: list[Email], now: datetime) -> tuple[int, int]:
    """(days_silent, prior_pings): silence since my last message; my messages in a row
    at the tail = follow-ups already sent without an answer."""
    if not emails:
        return 0, 0
    days = max(0, (now - emails[-1].sent_at).days)
    pings = 0
    for e in reversed(emails):
        if e.direction in _OUTBOUND:
            pings += 1
        else:
            break
    return days, pings


def _style_block(emails: list[Email]) -> str:
    """The user's own recent outbound messages from this thread — tone reference."""
    mine = [e for e in emails if e.direction in _OUTBOUND and (e.body_text or "").strip()]
    samples = [e.body_text.strip()[:_STYLE_CHARS] for e in mine[-_STYLE_SAMPLES:]]
    return "\n---\n".join(samples)


async def gather(session: AsyncSession, item: ActionItem) -> DraftContext | None:
    """Collect everything the prompt needs. Pure DB work — no LLM call here, so the
    caller can close the session before the slow network hop."""
    if item.type not in DRAFTABLE or item.source_thread_id is None:
        return None
    emails = list(
        (
            await session.execute(
                select(Email)
                .where(Email.thread_id == item.source_thread_id)
                .order_by(Email.sent_at)
            )
        ).scalars().all()
    )
    if not emails:
        return None
    thread = await session.get(Thread, item.source_thread_id)
    account_id = thread.account_id if thread else None
    space_id = await spaces_svc.space_of_account(session, account_id)
    days, pings = followup_stats(emails, datetime.now(UTC))
    memory = await memory_ctx.for_thread(
        session, thread_id=item.source_thread_id, emails=emails,
        identities=await get_identity_set(session), space_id=space_id,
        cards=True, exclude_item=item.id,
    )
    from email_reader import advice as advice_svc  # advice reads followup_stats from here

    plan = await advice_svc.cached_points(session, item, emails, memory)
    return DraftContext(
        item=item,
        thread_block=_render_tail(emails),
        profile=await profiles.core(session, space_id),
        style_block=_style_block(emails),
        days_silent=days,
        prior_pings=pings,
        account_id=account_id,
        memory_block=memory.render("draft"),
        plan=plan,
    )


_SYSTEM_COMMON = (
    "Ты — ассистент, который пишет черновик письма от имени владельца ящика. "
    "Пиши на языке последних писем треда. Если дан план ответа — покрой каждый его "
    "пункт и не добавляй того, чего в плане и переписке нет. Верни ТОЛЬКО текст письма: "
    "без темы, без подписи (она добавится автоматически) и без пояснений вокруг."
)

_SYSTEM_BY_TYPE = {
    ActionType.NEED_MY_REPLY: (
        "Задача: ответить по существу на последнее письмо, адресованное мне. "
        "Ответь на каждый заданный мне вопрос. Если данных не хватает — прямо скажи, "
        "что уточнишь, и задай встречный вопрос."
    ),
    ActionType.WAITING_FOR_REPLY: (
        "Задача: напомнить контрагенту, который не отвечает. Первый пинг — мягкий и "
        "короткий. Второй и далее — твёрже: конкретный срок ответа и что произойдёт, "
        "если ответа не будет (эскалация, сдвиг сроков)."
    ),
    ActionType.TO_DELEGATE: (
        "Задача: делегировать входящий запрос исполнителю. Напиши письмо исполнителю: "
        "краткий контекст, что именно сделать, срок и что вернуть мне. Постановка — "
        "конкретная, тон — уважительный."
    ),
}


def _user_prompt(ctx: DraftContext) -> str:
    parts = [f"Обязательство: {ctx.item.title}"]
    if ctx.item.reason:
        parts.append(f"Комментарий ассистента: {ctx.item.reason}")
    if ctx.item.type == ActionType.WAITING_FOR_REPLY:
        parts.append(
            f"Это пинг №{ctx.prior_pings + 1}; тишина {ctx.days_silent} дн."
        )
    if ctx.item.type == ActionType.TO_DELEGATE:
        parts.append(f"Кому делегировать: {ctx.item.delegate_to or 'выбери по профилю'}")
        if ctx.profile:
            parts.append(f"Профиль владельца ящика (роль и команды):\n{ctx.profile}")
    if ctx.memory_block:
        parts.append(ctx.memory_block.strip())
    if ctx.style_block:
        parts.append(f"Примеры моих писем (повтори тон и манеру):\n{ctx.style_block}")
    if ctx.plan:
        parts.append("План ответа (совет ассистента по уликам — покрой каждый пункт):\n"
                     + "\n".join(f"- {p}" for p in ctx.plan))
    parts.append(f"Переписка:\n{ctx.thread_block}")
    if ctx.instructions:
        parts.append(f"Что нужно сказать в ответе: {ctx.instructions}")
    parts.append("Напиши черновик.")
    return "\n\n".join(parts)


async def generate(rm, ctx: DraftContext) -> str | None:
    """LLM call (outside any DB session). None on failure — the UI degrades to an
    empty compose body with a hint, never an error page."""
    if not rm.api_key:
        return None
    system = f"{_SYSTEM_COMMON}\n{_SYSTEM_BY_TYPE[ctx.item.type]}"
    try:
        return await complete_text(
            kind=rm.kind, base_url=rm.base_url, api_key=rm.api_key, model=rm.model,
            no_thinking=rm.no_thinking,
            system=system, user=_user_prompt(ctx), max_tokens=900,
        )
    except Exception as exc:  # noqa: BLE001 — draft is a recommendation, not a must
        log.warning("Draft generation failed: %s", exc)
        return None
