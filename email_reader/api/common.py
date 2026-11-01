"""Shared module-level state and helpers used by the API routers."""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from email_reader import accounts as accounts_svc
from email_reader import attention as attention_svc
from email_reader import boards as boards_svc
from email_reader import clock, history, views
from email_reader import labels as labels_svc
from email_reader import maildrafts as maildrafts_svc
from email_reader import projects as projects_svc
from email_reader import spaces as spaces_svc
from email_reader.db import session_scope
from email_reader.enums import TYPE_LABEL as _TYPE_LABEL
from email_reader.paths import resource_path

# Read-only assets live next to the source in dev, and under sys._MEIPASS when frozen.
BASE_DIR = resource_path()
templates = Jinja2Templates(directory=str(resource_path("templates")))

VIEW_TITLES = {
    "today": "Сегодня",
    "needreply": "Ответить",
    "waiting": "Жду ответа",
    "delegated": "Делегировано",
    "commitments": "Мои задачи",
    "triage": "Разобрать",
    "snoozed": "Отложенные",
    "done": "Архив",
    "inbox": "Входящие",
    "sent": "Отправленные",
    "meetings": "Встречи",
}

# type -> (icon name for icons.icon(), semantic class). The icons match the sidebar, so
# a type is recognised by one image everywhere; the class picks the status colour.
TYPE_META = {
    "WAITING_FOR_REPLY": ("clock", "waiting"),
    "NEED_MY_REPLY": ("mail", "needreply"),
    "MY_COMMITMENT": ("check", "commitment"),
    "TASK": ("pen", "task"),
    "DELEGATED_TASK": ("forward", "delegated"),
    "TO_DELEGATE": ("forward", "delegated"),
    "INFO": ("dot", "info"),
    "MEETING_PREP": ("calendar", "info"),
    "DECISION": ("flag", "info"),
}

# Human labels for the raw enum names — shared with the bot, so they live in enums.
TYPE_LABEL = _TYPE_LABEL

# Почему ассистент предлагает закрыть карточку — тот же словарь, что читает история.
CLOSE_HINT = history._CLOSE_HINT


def _local(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Jinja filter: render a (UTC-aware) datetime in the system-local timezone.

    The desktop app stores UTC internally; every user-facing time goes through this
    filter so wall-clock never drifts by a day for UTC+N users."""
    if value is None:
        return ""
    return value.astimezone().strftime(fmt)


def _age(d: datetime | date | None) -> str:
    if d is None:
        return ""
    today = clock.today()
    dd = clock.today(d) if isinstance(d, datetime) else d
    delta = (dd - today).days
    if delta < 0:
        return "просрочено"
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    return f"{delta}д"


def _due_state(d: date | None) -> str:
    """'over' / 'today' / '' for a due date — the row's date cell colour and bold."""
    if d is None:
        return ""
    delta = (d - clock.today()).days
    return "over" if delta < 0 else ("today" if delta == 0 else "")


def _since(d: date | None) -> str:
    """«висит Nд»: how long the other side has kept me waiting. Empty when it began today."""
    if d is None:
        return ""
    n = (clock.today() - d).days
    return f"висит {n}д" if n > 0 else ""


_TIMECODE = re.compile(r"^(?:\s*\[\d{1,2}:\d{2}(?::\d{2})?\])+\s*")


# «Name (addr)» is how the engine stores a counterparty it read off the thread; the
# address is for disambiguation, not for the eye.
_ADDR_TAIL = re.compile(r"\s*\(([^()\s@]+@[^()\s]+)\)\s*$")


def _who_name(counterparty: str | None) -> str:
    """Jinja filter for the row's counterparty column: the name without the «(addr)»
    tail; a bare address stays. Display only — the pane and the dossier keep the value."""
    return _ADDR_TAIL.sub("", (counterparty or "").strip())


def _list_reason(reason: str | None) -> str:
    """Jinja filter for the row: the model's reason without the leading «[00:23:10]».
    Display only — the stored reason keeps the timecode, the pane links through it."""
    return _TIMECODE.sub("", reason or "").strip()


PAGE_SIZE = 50

# What the default («») grouping of a board is called in the group switcher.
GROUP_DEFAULT_LABEL = {
    "today": "срок", "waiting": "давность", "delegated": "тип", "commitments": "тип",
}
GROUP_LABELS = (("person", "человек"), ("project", "проект"), ("source", "источник"))


def _hl(value: str | None) -> Markup:
    """A search snippet: escape it — it is someone else's mail — then turn the two
    marker characters the index put around the match into <mark>."""
    from email_reader.search.query import HL_OFF, HL_ON

    out = escape(value or "")
    return Markup(str(out).replace(HL_ON, "<mark>").replace(HL_OFF, "</mark>"))


def _size(value: int | None) -> str:
    """Bytes as МБ/ГБ — storage numbers are only ever read as an order of magnitude."""
    n = float(value or 0)
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} ГБ"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} МБ"
    return f"{n / 1024:.0f} КБ"


def group_cookie(view: str) -> str:
    """The grouping is remembered PER board (a person-grouped «Делегировано» says nothing
    about how «Сегодня» should look), unlike the mailbox filter, which is shared."""
    return f"board_group_{view}"


# Which space the window is in — one cookie for every screen (docs/spaces.md §4). Set
# by the sidebar switcher; a missing or stale value means the default space.
SPACE_COOKIE = "space"


async def current_space(s, request: Request):
    return await spaces_svc.resolve(s, request.cookies.get(SPACE_COOKIE))


async def chip_in_space(s, space, account: str) -> tuple[list, uuid.UUID | None, str]:
    """The mailbox chip, validated against the space: a chip left over from another space
    (the cookie survives a switch) would make every board empty with nothing to click —
    a one-mailbox space shows no chips at all. Returns (mailboxes, account_id, account)."""
    accounts = await accounts_svc.list_accounts(s, space.id)
    try:
        acc_id = uuid.UUID(account) if account else None
    except ValueError:
        acc_id = None
    if acc_id is not None and acc_id not in {a.id for a in accounts}:
        acc_id = None
    return accounts, acc_id, (str(acc_id) if acc_id else "")


async def shell_ctx(s, request: Request) -> dict:
    """Всё, что рисует боковое меню: пространство и его соседи, счётчики досок, ящики,
    проекты. Один источник и для страницы, и для `/ui/sidebar` — иначе они расходятся, а
    страница отдаёт неполное меню, которое через мгновение подменяется полным, и меню
    дёргается на каждом переходе."""
    space = await current_space(s, request)
    cnt = await views.counts(s, space.id)
    cnt["inbox_unread"] = await views.unread_inbox_count(s)
    return {
        "space": space,
        "boards": await boards_svc.list_specs(s, space.id, visible_only=True),
        "spaces": await spaces_svc.list_spaces(s),
        "counts": cnt,
        "unread_by_account": await views.unread_inbox_by_account(s),
        "accounts": await accounts_svc.list_accounts(s, space.id),
        "projects": await projects_svc.list_projects(s, space.id),
        # Пилюли в сайдбаре только у Нитей, которые требуют меня или затухли.
        "thread_states": {
            t.project_id: t for t in await attention_svc.for_threads(s, space.id)
            if t.state in (attention_svc.ON_ME, attention_svc.IDLE)
        },
        "drafts_by_account": await maildrafts_svc.counts_by_account(s),
    }


async def _list_ctx(
    s, view: str, space, account: str, page: int = 0, group: str = ""
) -> dict:
    """Board context inside `space` (a Space row); `account` is the mailbox chip (""=all)."""
    spec = await boards_svc.get_spec(s, space.id, view)
    accounts, acc_id, account = await chip_in_space(s, space, account)
    page = max(page, 0)
    group = group if group in views.GROUPS else ""
    b = await views.board(
        s, view, space_id=space.id, account_id=acc_id,
        limit=PAGE_SIZE, offset=page * PAGE_SIZE, group=group or None,
    )
    items = [r.item for r in b.rows]
    total = b.total
    mids = [i.source_meeting_id for i in items if i.source_meeting_id]
    q = "&".join(
        p for p in (f"account={account}" if account else "", f"group={group}" if group else "")
        if p
    )
    # Срок, с которым открывается строка добавления. «Сегодня» — единственная доска,
    # которая без срока задачу не покажет: набранная там задача уезжала на «Мои задачи»
    # и исчезала из виду (docs/ux-tasks.md §17.7). Предзаполнение видно на кнопке и
    # снимается одним щелчком.
    qadd_due = clock.today() if view == "today" else None
    return {
        "view": view,
        "board_spec": spec,
        "qadd_due": qadd_due.isoformat() if qadd_due else "",
        "qadd_due_date": qadd_due,
        "board_title": spec.name if spec else VIEW_TITLES.get(view, view),
        "items": items,
        "rows": b.rows,
        "sections": b.sections,
        "group": group,
        "group_default_label": GROUP_DEFAULT_LABEL.get(view, "список"),
        "group_labels": GROUP_LABELS,
        "groupable": view in views._ORDERED_VIEWS,
        "item_sources": await views.sources_for_items(s, items),
        "item_labels": await labels_svc.labels_for_items(s, items),
        "item_meetings": await views.meetings_for_items(s, mids),
        "vip_threads": await views.vip_threads_of(s, items),
        "space": space,
        "accounts": accounts,
        "active_account": account,
        "p_page": page, "p_total": total, "p_size": PAGE_SIZE, "p_target": "#list",
        "p_url": f"/list/{view}" + (f"?{q}" if q else ""),
    }


# The board account filter is shared across ALL commitment boards and persists via a
# cookie, so switching boards keeps the selection (set only by the filter chips).
BOARD_ACCOUNT_COOKIE = "board_account"


async def _agent_status_ctx(s):
    return {"agent": await views.agent_status(s)}


async def _undo_payload(s, item_id: uuid.UUID, label: str | None, view: str) -> dict | None:
    """Before a resolving action: what the undo toast needs to put the item back. The
    state travels to the client and comes back with the undo — no server-side memory."""
    if not label:
        return None
    ai = await views.get_item(s, item_id)
    if ai is None:
        return None
    return {"id": str(item_id), "label": f"{label}: «{ai.title}»", "view": view,
            "prev": views.snapshot(ai)}


async def _advice_after(s, item_id: uuid.UUID) -> None:
    """Карточку изменили руками — пересчёт совета заказываем воркеру. Ушедшую с доски
    (готово, не обязательство) он не возьмёт: её текст замирает таким, каким был."""
    from email_reader import advice as advice_svc

    ai = await views.get_item(s, item_id)
    if ai is not None:
        await advice_svc.request(s, ai)


async def _refresh(
    request: Request, view: str, item_id: uuid.UUID, action, undo: str | None = None, **kw
):
    # Keep the shared board filter and the board's grouping applied when an item action
    # re-renders the list.
    account = request.cookies.get(BOARD_ACCOUNT_COOKIE, "")
    group = request.cookies.get(group_cookie(view), "")
    async with session_scope() as s:
        space = await current_space(s, request)
        undo_payload = await _undo_payload(s, item_id, undo, view)
        await action(s, item_id, **kw)
        await _advice_after(s, item_id)
        ctx = await _list_ctx(s, view, space, account, group=group)
    resp = templates.TemplateResponse(request, "_list.html", ctx)
    trigger: dict = {"countsChanged": True}
    if undo_payload:
        trigger["showUndo"] = undo_payload
    resp.headers["HX-Trigger"] = json.dumps(trigger)
    return resp


async def _refresh_advance(
    request: Request, view: str, item_id: uuid.UUID, action, undo: str | None = None, **kw
):
    """Like _refresh, but for resolving an item FROM its open reading pane: after the
    action, tell the client to open the next board item in the pane (or clear it when the
    board is now empty). The next item is picked before the action, while it's still listed."""
    account = request.cookies.get(BOARD_ACCOUNT_COOKIE, "")
    group = request.cookies.get(group_cookie(view), "")
    async with session_scope() as s:
        space = await current_space(s, request)
        _, acc_id, account = await chip_in_space(s, space, account)
        nxt = await views.next_open_item(s, view, space.id, acc_id, item_id)
        nxt_id = str(nxt.id) if nxt is not None else ""
        undo_payload = await _undo_payload(s, item_id, undo, view)
        await action(s, item_id, **kw)
        await _advice_after(s, item_id)
        ctx = await _list_ctx(s, view, space, account, group=group)
    resp = templates.TemplateResponse(request, "_list.html", ctx)
    trigger: dict = {"countsChanged": True, "openNext": {"id": nxt_id, "view": view}}
    if undo_payload:
        trigger["showUndo"] = undo_payload
    resp.headers["HX-Trigger"] = json.dumps(trigger)
    return resp
