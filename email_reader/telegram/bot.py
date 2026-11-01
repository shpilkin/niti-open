"""Telegram bots.

Two kinds live here:

* The GLOBAL bot (env TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_CHAT_ID): read-only views over
  every mailbox plus NL -> view mapping. Unchanged; notify.py sends alerts through it.
* One bot PER SPACE (telegram_bots: token + chat id, Настройки → Пространства): the same views
  scoped to that mailbox and — the point of it — intake: any text you send becomes a task
  in that mailbox's «Разобрать»; a forwarded message becomes a «нужен ответ» candidate
  from its original sender (docs/ux-tasks.md §4.4, §7.3). One Dispatcher polls all of
  them; a handler learns its mailbox from the bot the update arrived on.

Answers are built from DB rows, never generated, to avoid hallucinating commitments.
Inline buttons allow done / dismiss (and accept, for the inbox).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from email_reader import clock, views
from email_reader import spaces as spaces_svc
from email_reader.config import get_settings
from email_reader.db import session_scope
from email_reader.enums import TYPE_LABEL
from email_reader.telegram import intake

log = logging.getLogger("email_reader.telegram")

VIEW_LABEL = {
    "today": "Сегодня",
    "triage": "Разобрать",
    "needreply": "Нужен мой ответ",
    "waiting": "Жду ответа",
    "delegated": "Делегировано",
    "commitments": "Мои задачи",
}
COMMANDS = " ".join(f"/{v}" for v in VIEW_LABEL)

# NL keyword -> view (global bot only; a mailbox bot treats free text as a task).
_NL = [
    (("срочн", "сегодня", "today", "сделать"), "today"),
    (("не ответил", "жду", "waiting", "ждём", "ждем"), "waiting"),
    (("обещал", "обещан", "commit", "задач"), "commitments"),
    (("делегир", "deleg", "поручил"), "delegated"),
    (("мой ответ", "ответить", "reply", "needreply"), "needreply"),
    (("разобр", "triage", "входящ"), "triage"),
]

dp = Dispatcher()  # the global bot


def _allowed(message_or_cb) -> bool:
    allowed = get_settings().telegram_allowed_chat_id
    chat = message_or_cb.chat if isinstance(message_or_cb, Message) else message_or_cb.message.chat
    return allowed is None or chat.id == allowed


def _kb(item_id, *, inbox: bool = False) -> InlineKeyboardMarkup:
    row = []
    if inbox:
        row.append(InlineKeyboardButton(text="✓ На доску", callback_data=f"confirm:{item_id}"))
    row += [
        InlineKeyboardButton(text="✓ Готово", callback_data=f"done:{item_id}"),
        InlineKeyboardButton(text="✕ Не обязательство", callback_data=f"dismiss:{item_id}"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def _fmt(item) -> str:
    due = f" · до {item.due_date}" if item.due_date else ""
    who = f"{item.counterparty} · " if item.counterparty else ""
    return f"<b>{item.title}</b>\n{who}{TYPE_LABEL.get(item.type, item.type)}{due}"


async def _send_view(message: Message, view: str, space_id: uuid.UUID | None = None) -> None:
    async with session_scope() as s:
        items = await views.list_view(s, view, limit=15, space_id=space_id)
    label = VIEW_LABEL.get(view, view)
    if not items:
        await message.answer(f"<b>{label}</b>: пусто ✨", parse_mode="HTML")
        return
    await message.answer(f"<b>{label}</b> ({len(items)}):", parse_mode="HTML")
    for item in items:
        await message.answer(
            _fmt(item), parse_mode="HTML", reply_markup=_kb(item.id, inbox=(view == "triage"))
        )


def _parse_cb(cb: CallbackQuery) -> tuple[str, uuid.UUID | None]:
    action, _, raw_id = (cb.data or "").partition(":")
    try:
        return action, uuid.UUID(raw_id)
    except ValueError:
        return action, None


async def _apply(action: str, item_id: uuid.UUID) -> str:
    async with session_scope() as s:
        if action == "done":
            await views.complete(s, item_id)
            return "Готово ✓"
        if action == "confirm":
            await views.confirm(s, item_id)
            return "На доске ✓"
        await views.dismiss(s, item_id)
        return "Отклонено ✕"


async def _handle_cb(cb: CallbackQuery) -> None:
    action, item_id = _parse_cb(cb)
    if item_id is None:
        await cb.answer("Ошибка")
        return
    await cb.answer(await _apply(action, item_id))
    if cb.message:
        await cb.message.edit_reply_markup(reply_markup=None)


# ---- the global bot -------------------------------------------------------------


def _make_view_handler(view: str):
    async def handler(message: Message) -> None:
        if not _allowed(message):
            return
        await _send_view(message, view)

    return handler


@dp.message(F.text & ~F.text.startswith("/"))
async def nl_handler(message: Message) -> None:
    if not _allowed(message):
        return
    text = (message.text or "").lower()
    for keywords, view in _NL:
        if any(k in text for k in keywords):
            await _send_view(message, view)
            return
    await message.answer(f"Не понял. Команды: {COMMANDS}")


@dp.callback_query(F.data.startswith(("done:", "dismiss:", "confirm:")))
async def action_cb(cb: CallbackQuery) -> None:
    if not _allowed(cb):
        await cb.answer("Нет доступа")
        return
    await _handle_cb(cb)


_registered: set[str] = set()


def _register_commands() -> None:
    # The supervisor restarts run_bot()/run_bots() after a crash; a second registration
    # would make every command answer twice.
    if "global" in _registered:
        return
    _registered.add("global")
    for view in VIEW_LABEL:
        dp.message.register(_make_view_handler(view), Command(view))


async def run_bot() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN not set; global Telegram bot disabled")
        return
    _register_commands()
    bot = Bot(token=settings.telegram_bot_token)
    log.info("Telegram bot started")
    await dp.start_polling(bot)


# ---- one bot per space ------------------------------------------------------------


@dataclass(frozen=True)
class BotSpace:
    space_id: uuid.UUID
    name: str
    chat_id: int | None


acc_dp = Dispatcher()


def _acc_of(bot: Bot) -> BotSpace | None:
    """Which space the bot an update arrived on feeds (set by run_bots)."""
    return acc_dp.workflow_data.get("accounts", {}).get(bot.id)


def _acc_allowed(event, bot: Bot) -> BotSpace | None:
    """The space, if this update comes from the one chat its bot is allowed to serve.
    No chat id configured -> the bot answers nothing but /start."""
    acc = _acc_of(bot)
    if isinstance(event, Message):
        chat = event.chat
    else:
        chat = event.message.chat if event.message else None
    if acc is None or chat is None or acc.chat_id is None or chat.id != acc.chat_id:
        return None
    return acc


@acc_dp.message(Command("start"))
async def acc_start(message: Message, bot: Bot) -> None:
    acc = _acc_of(bot)
    if acc is None:
        return
    if acc.chat_id == message.chat.id:
        await message.answer(
            f"Это бот пространства <b>{acc.name}</b>. Напишите задачу — она попадёт в "
            f"«Разобрать»; "
            f"перешлите сообщение — станет «нужен ответ» от его автора.\nКоманды: {COMMANDS}",
            parse_mode="HTML",
        )
        return
    # Not linked yet (or linked to another chat): tell the user what to paste where.
    # The chat id is the user's own; nothing about the space is revealed.
    await message.answer(
        f"Chat id этого чата: <code>{message.chat.id}</code>. Вставьте его в настройки "
        f"бота пространства «{acc.name}» (Настройки → Пространства) — бот подхватит его "
        f"в течение минуты.",
        parse_mode="HTML",
    )


def _make_acc_view_handler(view: str):
    async def handler(message: Message, bot: Bot) -> None:
        acc = _acc_allowed(message, bot)
        if acc is None:
            return
        await _send_view(message, view, space_id=acc.space_id)

    return handler


@acc_dp.message(F.text & ~F.text.startswith("/"))
async def acc_intake(message: Message, bot: Bot) -> None:
    acc = _acc_allowed(message, bot)
    if acc is None:
        return
    async with session_scope() as s:
        ai = await intake.ingest(
            s, space_id=acc.space_id, chat_id=message.chat.id,
            message_id=message.message_id, text=message.text or "",
            forwarded_from=intake.origin_name(message), today=clock.today(),
        )
    if ai is None:
        await message.answer("Пусто — нечего добавлять.")
        return
    await message.answer(
        f"В «Разобрать» ({acc.name}):\n{_fmt(ai)}", parse_mode="HTML",
        reply_markup=_kb(ai.id, inbox=True),
    )


@acc_dp.callback_query(F.data.startswith(("done:", "dismiss:", "confirm:")))
async def acc_action_cb(cb: CallbackQuery, bot: Bot) -> None:
    if _acc_allowed(cb, bot) is None:
        await cb.answer("Нет доступа")
        return
    await _handle_cb(cb)


def _register_acc_commands() -> None:
    if "accounts" in _registered:
        return
    _registered.add("accounts")
    for view in VIEW_LABEL:
        acc_dp.message.register(_make_acc_view_handler(view), Command(view))


async def _bot_accounts() -> dict[str, BotSpace]:
    """token -> space, for every enabled bot."""
    async with session_scope() as s:
        out: dict[str, BotSpace] = {}
        for b in await spaces_svc.active_bots(s):
            token = spaces_svc.resolve_bot_token(b)
            if not token:
                continue
            space = await spaces_svc.get_space(s, b.space_id)
            out[token] = BotSpace(b.space_id, space.name if space else b.name, b.chat_id)
        return out


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_bots(stop: asyncio.Event) -> None:
    """Poll every enabled space bot, all on one dispatcher.

    The bots are re-read every minute: a changed chat id is picked up in place; a
    changed SET of tokens (bot added, removed, replaced) stops polling and starts it
    again with the new set, so no restart of the app is needed for either."""
    _register_acc_commands()
    while not stop.is_set():
        accounts = await _bot_accounts()
        bots: list[Bot] = []
        for token, acc in accounts.items():
            try:
                bots.append(Bot(token=token))
            except Exception:  # noqa: BLE001 — a malformed token must not kill the others
                log.warning("Telegram: the bot token of space %s is not a token", acc.name)
        if not bots:
            await _wait(stop, 60)
            continue
        acc_dp.workflow_data["accounts"] = {b.id: accounts[b.token] for b in bots}
        log.info("Telegram: polling %d space bot(s)", len(bots))
        poll = asyncio.create_task(acc_dp.start_polling(*bots, handle_signals=False))
        try:
            while not poll.done():
                await _wait(stop, 60)
                if stop.is_set():
                    break
                fresh = await _bot_accounts()
                if set(fresh) != set(accounts):
                    log.info("Telegram: space bots changed — restarting polling")
                    break
                acc_dp.workflow_data["accounts"] = {
                    b.id: fresh[b.token] for b in bots if b.token in fresh
                }
            if not poll.done():
                await acc_dp.stop_polling()
            await poll
        finally:
            for b in bots:
                await b.session.close()
