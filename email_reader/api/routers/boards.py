"""Commitment boards: views/lists, item panes and actions, dossier, weekly review."""

from __future__ import annotations

import json
import re
import uuid
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from email_reader import advice as advice_svc
from email_reader import boards as boards_svc
from email_reader import clock, history, quickadd, views
from email_reader import compose as compose_svc
from email_reader import drafts as drafts_svc
from email_reader import meetings as meetings_svc
from email_reader import model_connections as mc_svc
from email_reader import signatures as signatures_svc
from email_reader import spaces as spaces_svc
from email_reader.api.common import (
    BOARD_ACCOUNT_COOKIE,
    SPACE_COOKIE,
    VIEW_TITLES,
    _age,
    _list_ctx,
    _refresh,
    _refresh_advance,
    current_space,
    group_cookie,
    shell_ctx,
    templates,
)
from email_reader.db import session_scope
from email_reader.memory import remember as remember_svc

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse("/view/today")


@router.get("/api/notifications")
async def api_notifications():
    """Machine-readable feed for the native shell: badge counts + actionable items.

    The shell polls this, shows a system notification per newly-seen item id,
    and sets the dock badge from counts['today'].
    """
    async with session_scope() as s:
        cnt = await views.counts(s)
        items = await views.list_view(s, "today")
    return {
        "counts": cnt,
        "items": [
            {
                "id": str(i.id),
                "title": i.title,
                "type": i.type,
                "counterparty": i.counterparty,
            }
            for i in items
        ],
    }


@router.get("/view/{view}", response_class=HTMLResponse)
async def view_page(
    request: Request, view: str, account: str | None = None, page: int = 0
):
    # No explicit ?account (sidebar board switch) -> apply the remembered filter.
    acc = account if account is not None else request.cookies.get(BOARD_ACCOUNT_COOKIE, "")
    group = request.cookies.get(group_cookie(view), "")
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
        # Доска, которой в этом пространстве нет (спрятали, удалили, ссылка из другого
        # пространства), ведёт на первую видимую, а не молча на «Сегодня».
        if await boards_svc.get_spec(s, shell["space"].id, view) is None:
            landing = await boards_svc.landing_key(s, shell["space"].id)
            if view != landing:
                return RedirectResponse(f"/view/{landing}", status_code=303)
            view = landing
        ctx = {**shell, **await _list_ctx(s, view, shell["space"], acc, page, group=group)}
        ctx["model_configured"] = await mc_svc.is_model_configured(s)
    return templates.TemplateResponse(request, "index.html", ctx)


# Screens that exist in every space. Anything else — a project, a meeting, a mailbox, an
# item pane — belongs to ONE space and must not be shown under another's sidebar.
_SAFE_NEXT = re.compile(
    r"^/(view/[a-z0-9-]+|meetings|people|projects|review|mail/(inbox|sent)|settings(/.*)?)$"
)


def safe_next(path: str) -> str:
    """Where the switcher lands after changing space: the same screen if it is space-
    independent, «Сегодня» otherwise."""
    path = (path or "").split("?", 1)[0]
    return path if _SAFE_NEXT.match(path) else "/view/today"


@router.post("/space/{space_id}")
async def switch_space(request: Request, space_id: uuid.UUID, next: str = Form("/view/today")):
    """The sidebar switcher: remember the space for every screen and go back to it."""
    async with session_scope() as s:
        space = await spaces_svc.get_space(s, space_id)
    resp = RedirectResponse(safe_next(next), status_code=303)
    if space is not None:
        resp.set_cookie(SPACE_COOKIE, str(space.id), max_age=31_536_000, path="/",
                        samesite="lax")
    return resp


@router.get("/list/{view}", response_class=HTMLResponse)
async def list_partial(
    request: Request, view: str, account: str | None = None, page: int = 0,
    group: str | None = None,
):
    """A list swap. The toolbar's mailbox select sends `account` ("" = all) and it is
    remembered for every board; a swap without it (board switch, grouping, paging) keeps
    the remembered one. `group` is per board, same rule."""
    remember = {}
    if account is None:
        account = request.cookies.get(BOARD_ACCOUNT_COOKIE, "")
    else:
        remember[BOARD_ACCOUNT_COOKIE] = account
    if group is None:
        group = request.cookies.get(group_cookie(view), "")
    else:
        remember[group_cookie(view)] = group
    async with session_scope() as s:
        space = await current_space(s, request)
        ctx = await _list_ctx(s, view, space, account, page, group=group)
    resp = templates.TemplateResponse(request, "_list.html", ctx)
    for key, value in remember.items():
        resp.set_cookie(key, value, max_age=31_536_000, path="/", samesite="lax")
    return resp


@router.get("/item/{item_id}", response_class=HTMLResponse)
async def item_pane(request: Request, item_id: uuid.UUID, view: str = "today"):
    async with session_scope() as s:
        ai = await views.get_item(s, item_id)
        messages = (
            await views.get_thread_messages(s, ai.source_thread_id)
            if ai and ai.source_thread_id
            else []
        )
        source = None
        if ai:
            src = (await views.sources_for_items(s, [ai])).get(ai.id)
            source = src[1] if src else None
        # An item spoken at a meeting has no thread to show; the way back to what was
        # actually said is the meeting itself, so the pane links to it.
        meeting = (
            await meetings_svc.get_meeting(s, ai.source_meeting_id)
            if ai and ai.source_meeting_id
            else None
        )
        remember_addr, remember_name = (
            await remember_svc.counterparty_target(s, ai) if ai else (None, None)
        )
    return templates.TemplateResponse(
        request, "_pane.html",
        {"item": ai, "messages": messages, "view": view, "source": source,
         "meeting": meeting, "remember_addr": remember_addr, "remember_name": remember_name,
         "at": meetings_svc.quoted_timecode(ai.reason) if meeting else None,
         "advisable": advice_svc.advisable(ai)},
    )


@router.get("/item/{item_id}/advice", response_class=HTMLResponse)
async def item_advice(request: Request, item_id: uuid.UUID, view: str = "today", refresh: int = 0):
    """Совет по карточке — тот, что уже посчитан. Модель отсюда не зовём: вызов занимает
    секунды, а в очереди — минуты, и всё это время панель ждала бы его, держа соединение
    (docs/feature-card-advice.md). Считает воркер по событию; `refresh=1` — заказать новый
    счёт, а не дождаться его здесь. Степень важности — правило по данным карточки, поэтому
    её считаем сейчас, а не берём из сохранённого: срок наступает сам."""
    async with session_scope() as s:
        ai = await views.get_item(s, item_id)
        if ai is None or not advice_svc.advisable(ai):
            return HTMLResponse("")  # nothing to advise on — the placeholder goes away
        if refresh:
            await advice_svc.request(s, ai)
        advice = await advice_svc.latest(s, ai.id)
        state = await advice_svc.state(s, ai.id)
        last = await advice_svc.last_email(s, ai)
        rm = await mc_svc.resolve_for_thread(s, ai.source_thread_id)
    imp = advice_svc.importance(ai, last, vip=advice.vip if advice else False)
    return templates.TemplateResponse(
        request, "_advice.html",
        {"item": ai, "advice": advice, "view": view, "state": state,
         "importance": imp, "importance_label": advice_svc.IMPORTANCE_LABEL[imp],
         "no_model": not rm.api_key},
    )


@router.get("/review", response_class=HTMLResponse)
async def weekly_review_page(request: Request):
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
        data = await views.weekly_review(s, shell["space"].id)
    return templates.TemplateResponse(
        request, "review.html",
        {**shell, "view": "review", "title_override": "Итоги недели",
         "active_key": "review", **data},
    )


@router.post("/item/{item_id}/keep", response_class=HTMLResponse)
async def item_keep_open(request: Request, item_id: uuid.UUID, view: str = Form("today")):
    """«Оставить» в секции «Предлагаю закрыть»: обязательство живо, снимаем предложение.
    Строка возвращается в свою обычную секцию, отсчёт старения начинается заново."""
    return await _refresh(request, view, item_id, views.keep_open)


@router.get("/item/{item_id}/history", response_class=HTMLResponse)
async def item_history(request: Request, item_id: uuid.UUID):
    """What happened to this card: type changes the assistant made, its own closes, and my
    own actions. Read from the decision journal (docs/ux-tasks.md §17)."""
    async with session_scope() as s:
        ai = await views.get_item(s, item_id)
        if ai is None:
            return HTMLResponse("<div class='pane-empty'>Карточка не найдена.</div>", 404)
        entries = await history.for_item(s, item_id)
    return templates.TemplateResponse(
        request, "_itemhistory.html", {"item": ai, "entries": entries},
    )


@router.get("/item/{item_id}/draft", response_class=HTMLResponse)
async def item_draft(request: Request, item_id: uuid.UUID, view: str = "today"):
    """Assistant-recommended draft: opens the regular compose form with a generated
    body above the quote. Reply flow for NEED_MY_REPLY/WAITING_FOR_REPLY, forward
    flow for TO_DELEGATE (the delegate gets the thread context). Never auto-sends."""
    async with session_scope() as s:
        ai = await views.get_item(s, item_id)
        if not ai or ai.type not in drafts_svc.DRAFTABLE or not ai.source_thread_id:
            return HTMLResponse("<div class='pane-empty'>Черновик недоступен.</div>")
        dctx = await drafts_svc.gather(s, ai)
        rm = await mc_svc.resolve_for_account(s, dctx.account_id if dctx else None)
        if ai.type == "TO_DELEGATE":
            ctx = await compose_svc.forward_context_thread(s, ai.source_thread_id)
        else:
            ctx = await compose_svc.reply_context_thread(s, ai.source_thread_id)
        accs = await compose_svc.enabled_accounts(s, ai.space_id)
        sigs = await signatures_svc.for_accounts(s, accs)
        sig_texts = signatures_svc.to_texts(sigs)
        sig_fonts = signatures_svc.to_fonts(sigs)
    # LLM call OUTSIDE the session: it is slow and needs no DB.
    text = await drafts_svc.generate(rm, dctx) if dctx else None
    if ctx is None:
        return HTMLResponse("<div class='pane-empty'>Черновик недоступен.</div>")
    ctx["draft"] = text or (
        "(Не удалось сгенерировать черновик — проверь модель в Настройках → AI.)"
    )
    return templates.TemplateResponse(
        request, "_compose.html",
        {"accounts": accs, "ctx": ctx,
         "back_url": f"/item/{item_id}?view={view}", "signatures": sigs,
         "sig_texts": sig_texts, "sig_fonts": sig_fonts},
    )


# `advance=1` (sent only by the buttons inside the open reading pane) makes the response
# also open the next board item in the pane; the inline list buttons omit it and just
# re-render the list.
@router.post("/item/{item_id}/complete", response_class=HTMLResponse)
async def complete(
    request: Request, item_id: uuid.UUID, view: str = Form("today"), advance: int = Form(0)
):
    refresh = _refresh_advance if advance else _refresh
    return await refresh(request, view, item_id, views.complete, undo="Готово")


@router.post("/item/{item_id}/dismiss", response_class=HTMLResponse)
async def dismiss(
    request: Request, item_id: uuid.UUID, view: str = Form("today"), advance: int = Form(0)
):
    refresh = _refresh_advance if advance else _refresh
    return await refresh(request, view, item_id, views.dismiss, undo="Не обязательство")


@router.post("/item/{item_id}/duplicate", response_class=HTMLResponse)
async def duplicate(
    request: Request, item_id: uuid.UUID, target: str = Form(""),
    view: str = Form("today"), advance: int = Form(0),
):
    """The advice's one click: this card is a duplicate of `target`, which keeps the
    obligation (docs/feature-card-advice.md §1.4)."""
    try:
        target_id = uuid.UUID(target)
    except ValueError:
        return HTMLResponse("<div class='pane-empty'>Карточка-оригинал не указана.</div>", 400)
    refresh = _refresh_advance if advance else _refresh
    return await refresh(
        request, view, item_id, views.close_duplicate, undo="Закрыто как дубль",
        target_id=target_id,
    )


@router.post("/item/{item_id}/confirm", response_class=HTMLResponse)
async def confirm(request: Request, item_id: uuid.UUID, view: str = Form("today")):
    return await _refresh(request, view, item_id, views.confirm)


@router.post("/item/{item_id}/snooze", response_class=HTMLResponse)
async def snooze(
    request: Request, item_id: uuid.UUID, view: str = Form("today"),
    days: int = Form(3), until: str = Form(""), advance: int = Form(0),
):
    until_date = None
    if until:
        try:
            until_date = date.fromisoformat(until)
        except ValueError:
            pass  # bad manual input -> fall back to the days offset
    refresh = _refresh_advance if advance else _refresh
    return await refresh(
        request, view, item_id, views.snooze, undo="Отложено", days=days, until=until_date
    )


@router.post("/item/{item_id}/undo", response_class=HTMLResponse)
async def undo(
    request: Request, item_id: uuid.UUID, view: str = Form("today"), prev: str = Form("{}")
):
    """Put a just-resolved item back (the toast's «Отменить»). `prev` is the snapshot the
    resolving response handed to the client; views.restore validates every field."""
    try:
        payload = json.loads(prev)
    except ValueError:
        payload = None
    account = request.cookies.get(BOARD_ACCOUNT_COOKIE, "")
    group = request.cookies.get(group_cookie(view), "")
    async with session_scope() as s:
        space = await current_space(s, request)
        ok = await views.restore(s, item_id, payload) if payload is not None else False
        ctx = await _list_ctx(s, view, space, account, group=group)
    resp = templates.TemplateResponse(request, "_list.html", ctx)
    resp.headers["HX-Trigger"] = json.dumps(
        {"countsChanged": True, "showToast": "Возвращено" if ok else "Не удалось вернуть"}
    )
    return resp


@router.post("/item/{item_id}/due", response_class=HTMLResponse)
async def set_due(
    request: Request, item_id: uuid.UUID, view: str = Form("today"), until: str = Form("")
):
    """Deadline from the row's date popover: a date, or empty to clear it."""
    due = None
    if until.strip():
        try:
            due = date.fromisoformat(until.strip())
        except ValueError:
            return Response(
                status_code=204,
                headers={"HX-Trigger": json.dumps({"showToast": "Непонятная дата"})},
            )
    return await _refresh(request, view, item_id, views.set_due, due=due)


@router.post("/items/bulk", response_class=HTMLResponse)
async def bulk(
    request: Request, ids: str = Form(""), action: str = Form(...), view: str = Form("today"),
    days: int = Form(3), type: str = Form(""),
):
    """One action for every checked row. No undo — the toast says how many."""
    id_list = []
    for raw in ids.split(","):
        try:
            id_list.append(uuid.UUID(raw.strip()))
        except ValueError:
            continue
    account = request.cookies.get(BOARD_ACCOUNT_COOKIE, "")
    group = request.cookies.get(group_cookie(view), "")
    async with session_scope() as s:
        space = await current_space(s, request)
        try:
            n = await views.bulk(s, id_list, action, days=days, new_type=type or None)
        except ValueError as exc:
            return Response(
                status_code=204, headers={"HX-Trigger": json.dumps({"showToast": str(exc)})}
            )
        ctx = await _list_ctx(s, view, space, account, group=group)
    resp = templates.TemplateResponse(request, "_list.html", ctx)
    verb = {"complete": "Готово", "snooze": "Отложено", "dismiss": "Не обязательство",
            "retype": "Тип изменён", "confirm": "На доску"}[action]
    resp.headers["HX-Trigger"] = json.dumps(
        {"countsChanged": True, "showToast": f"{verb}: {n}"}
    )
    return resp


@router.post("/item/{item_id}/edit", response_class=HTMLResponse)
async def edit(
    request: Request,
    item_id: uuid.UUID,
    view: str = Form("today"),
    type_: str = Form(...),
    title: str = Form(...),
    counterparty: str = Form(""),
    due_date: str = Form(""),
):
    dd = None
    if due_date.strip():
        try:
            dd = date.fromisoformat(due_date.strip())
        except ValueError:
            dd = None
    return await _refresh(
        request,
        view,
        item_id,
        views.edit,
        type_=type_,
        title=title,
        counterparty=counterparty.strip() or None,
        due_date=dd,
    )


@router.post("/item/{item_id}/retype", response_class=HTMLResponse)
async def retype(
    request: Request, item_id: uuid.UUID, view: str = Form("today"),
    type: str = Form(...),
):
    return await _refresh(request, view, item_id, views.set_type, new_type=type)


def _iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


@router.post("/items/quick", response_class=HTMLResponse)
async def quick_add(
    request: Request, text: str = Form(""), view: str = Form("today"),
    due: str = Form(""), due_picked: str = Form("0"),
):
    """One typed line -> a confirmed TASK on «Мои задачи» of the open space.
    The date phrase («до пятницы», «15.10») becomes the due date (quickadd.parse).

    Срок может прийти и кнопкой. Чей срок выигрывает: выбранный руками (due_picked) —
    всегда; иначе набранная в строке фраза; предзаполнение доски — только если фразы
    не было. Иначе «до пятницы», набранное на «Сегодня», молча становилось сегодняшним."""
    text = text.strip()
    if not text:
        return Response(
            status_code=204, headers={"HX-Trigger": json.dumps({"showToast": "Пустая строка"})}
        )
    title, final = quickadd.resolve(
        text, clock.today(), chosen=_iso_date(due), picked=due_picked == "1"
    )
    board_account = request.cookies.get(BOARD_ACCOUNT_COOKIE, "")
    group = request.cookies.get(group_cookie(view), "")
    async with session_scope() as s:
        space = await current_space(s, request)
        item = await views.create_task(s, title=title, due=final, space_id=space.id)
        item_id = str(item.id)
        # Куда карточка попала — по записи доски, а не по строке в коде: доски
        # переименовываются в настройках, и подсказка обязана называть то же имя.
        home = await boards_svc.get_spec(s, space.id, "commitments")
        home_name = home.name if home is not None else VIEW_TITLES["commitments"]
        ctx = await _list_ctx(s, view, space, board_account, group=group)
    resp = templates.TemplateResponse(request, "_list.html", ctx)
    when = f" · {_age(final)}" if final else ""
    resp.headers["HX-Trigger"] = json.dumps({"countsChanged": True})
    # После подмены списка: если строка на этой доске — подсветить её, если нет —
    # сказать, на какой она доске, и дать туда перейти (docs/ux-tasks.md §17.7).
    resp.headers["HX-Trigger-After-Swap"] = json.dumps(
        {"taskAdded": {
            "id": item_id, "title": title, "when": when,
            "board_key": "commitments", "board_name": home_name,
        }}
    )
    return resp


@router.post("/item/from-email/{email_id}")
async def item_from_email(
    request: Request, email_id: uuid.UUID, type: str = Form(...)
):
    async with session_scope() as s:
        ai = await views.create_item_from_email(s, email_id, type)
    msg = "Обязательство создано" if ai is not None else "Не удалось создать"
    trigger = json.dumps({"showToast": msg, "countsChanged": True})
    return Response(content="", status_code=200, headers={"HX-Trigger": trigger})


@router.get("/counts", response_class=HTMLResponse)
async def counts_partial(request: Request, view: str = "today"):
    async with session_scope() as s:
        space = await current_space(s, request)
        cnt = await views.counts(s, space.id)
        specs = await boards_svc.list_specs(s, space.id, visible_only=True)
    return templates.TemplateResponse(
        request, "_counts.html", {"counts": cnt, "view": view, "boards": specs}
    )
