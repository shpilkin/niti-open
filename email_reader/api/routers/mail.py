"""Mailbox views: mail lists, reading panes, attachments, read state.

Search lives in `routers/search.py` — it spans every kind of thing, not only mail."""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from email_reader import accounts as accounts_svc
from email_reader import attachments as attachments_svc
from email_reader import htmlview, views
from email_reader import labels as labels_svc
from email_reader import maildrafts as maildrafts_svc
from email_reader import projects as projects_svc
from email_reader import spaces as spaces_svc
from email_reader.api.common import PAGE_SIZE, current_space, shell_ctx, templates
from email_reader.db import session_scope

router = APIRouter()
log = logging.getLogger("email_reader.mail")

THREAD_ROWS_LIMIT = 100  # беседа длиннее сотни писем — редкость, а не список


async def _label_email_ids(s, label: str) -> list | None:
    """Письма под меткой, или None когда метки нет — метка стоит на письме, не на беседе."""
    if not label:
        return None
    lbl = next((x for x in await labels_svc.list_labels(s) if str(x.id) == label), None)
    return await labels_svc.email_ids_for_label(s, lbl.id) if lbl is not None else None


async def _list_ctx(
    s, box: str, *, page: int, list_url: str, space_id=None, account_id=None,
    email_ids=None,
) -> dict:
    """Всё, что рисует список писем: сами письма, метки, размеры бесед и страницы.

    Один источник для страницы и для её обновления после отправки — иначе список,
    пришедший по htmx, расходится с тем, что отрисовала страница."""
    if await views.mail_grouped(s):
        emails, sizes = await views.list_mail_threads(
            s, box, email_ids=email_ids, account_id=account_id, space_id=space_id,
            limit=PAGE_SIZE, offset=page * PAGE_SIZE,
        )
        total = await views.count_mail_threads(
            s, box, email_ids=email_ids, account_id=account_id, space_id=space_id
        )
    else:
        emails, sizes = await views.list_mail(
            s, box, email_ids=email_ids, account_id=account_id, space_id=space_id,
            limit=PAGE_SIZE, offset=page * PAGE_SIZE,
        ), {}
        total = await views.count_mail(
            s, box, email_ids=email_ids, account_id=account_id, space_id=space_id
        )
    return {
        "emails": emails,
        "email_labels": await labels_svc.labels_for_emails(s, [e.id for e in emails]),
        "thread_sizes": sizes,
        "list_url": list_url,
        "p_page": page, "p_total": total, "p_size": PAGE_SIZE,
    }


@router.get("/mail/{box}", response_class=HTMLResponse)
async def mail_page(request: Request, box: str, label: str = "", page: int = 0):
    if box not in ("inbox", "sent"):
        box = "inbox"
    page = max(page, 0)
    title_override = None
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
        space = shell["space"]
        email_ids = None
        if label:
            lbl = next(
                (x for x in await labels_svc.list_labels(s) if str(x.id) == label), None
            )
            if lbl is not None:
                email_ids = await labels_svc.email_ids_for_label(s, lbl.id)
                title_override = f"Метка: {lbl.name}"
        suffix = f"?label={label}" if label else ""
        listing = await _list_ctx(
            s, box, page=page, space_id=space.id, email_ids=email_ids,
            list_url=f"/mail/{box}/list{suffix}",
        )
        cnt = await views.counts(s, space.id)
    return templates.TemplateResponse(
        request,
        "mail.html",
        {
            **shell, **listing,
            "view": box,
            "counts": cnt,
            "title_override": title_override,
            "p_url": f"/mail/{box}{suffix}",
        },
    )


@router.get("/mail/account/{account_id}/{box}", response_class=HTMLResponse)
async def account_mail_page(
    request: Request, account_id: uuid.UUID, box: str, page: int = 0
):
    boxes = ("inbox", "sent", "drafts")
    if box not in boxes:
        box = "inbox"
    page = max(page, 0)
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
        acc = await accounts_svc.get_account(s, account_id)
        if acc is None:
            return RedirectResponse("/view/today", status_code=303)
        if box == "drafts":
            return templates.TemplateResponse(
                request, "mail.html",
                {
                    **shell, "view": "drafts", "emails": [],
                    "drafts": await maildrafts_svc.for_account(s, acc.id),
                    "drafts_account_id": acc.id,
                    "counts": await views.counts(s, acc.space_id),
                    "title_override": f"{acc.name} · Черновики",
                    "active_key": f"acct:{account_id}:drafts",
                    "sync_account_id": account_id,
                },
            )
        listing = await _list_ctx(
            s, box, page=page, account_id=account_id,
            list_url=f"/mail/account/{account_id}/{box}/list",
        )
        cnt = await views.counts(s, acc.space_id)
    box_label = "Входящие" if box == "inbox" else "Отправленные"
    return templates.TemplateResponse(
        request,
        "mail.html",
        {
            **shell, **listing,
            "view": box,
            "counts": cnt,
            "title_override": f"{acc.name} · {box_label}",
            "active_key": f"acct:{account_id}:{box}",
            "sync_account_id": account_id,  # enables the per-mailbox "Синхронизировать"
            "p_url": f"/mail/account/{account_id}/{box}",
        },
    )


@router.get("/mail/{box}/list", response_class=HTMLResponse)
async def mail_list(request: Request, box: str, label: str = "", page: int = 0):
    """Только список — страница перечитывает его после отправки.

    Без этого отправленное письмо не появлялось в «Отправленных» до перезагрузки:
    счётчик в меню сдвигался, а строк в списке не прибавлялось."""
    if box not in ("inbox", "sent"):
        box = "inbox"
    page = max(page, 0)
    async with session_scope() as s:
        space = await current_space(s, request)
        email_ids = await _label_email_ids(s, label)
        suffix = f"?label={label}" if label else ""
        ctx = await _list_ctx(
            s, box, page=page, space_id=space.id, email_ids=email_ids,
            list_url=f"/mail/{box}/list{suffix}",
        )
    return templates.TemplateResponse(
        request, "_maillist.html",
        {**ctx, "view": box, "p_url": f"/mail/{box}{suffix}"},
    )


@router.post("/mail/account/{account_id}/sync", response_class=HTMLResponse)
async def account_sync_now(account_id: uuid.UUID):
    """Sync just this mailbox (fire-and-forget in the shared loop)."""
    import asyncio

    from email_reader.config import get_app_config
    from email_reader.connectors.sync_worker import sync_once

    async with session_scope() as s:
        acc = await accounts_svc.get_account(s, account_id)
    if acc is None:
        return HTMLResponse("Ящик не найден", status_code=404)
    asyncio.create_task(sync_once(get_app_config(), account_id=account_id))
    return HTMLResponse("<span class='export-ok'>⟳ Синхронизация запущена…</span>")


@router.get("/mail/account/{account_id}/drafts/list", response_class=HTMLResponse)
async def drafts_list(request: Request, account_id: uuid.UUID):
    """Список черновиков отдельно от страницы: отправка удаляет черновик, и без
    перерисовки на экране остаётся строка того, что уже ушло."""
    async with session_scope() as s:
        rows = await maildrafts_svc.for_account(s, account_id)
    return templates.TemplateResponse(
        request, "_draftlist.html", {"drafts": rows, "drafts_account_id": account_id}
    )


@router.post("/drafts/{draft_id}/delete", response_class=HTMLResponse)
async def draft_delete(request: Request, draft_id: uuid.UUID, account_id: uuid.UUID):
    async with session_scope() as s:
        gone = await maildrafts_svc.discard(s, draft_id)
        rows = await maildrafts_svc.for_account(s, account_id)
    resp = templates.TemplateResponse(
        request, "_draftlist.html",
        {"drafts": rows, "drafts_account_id": account_id},
    )
    if gone:
        # Папка исчезает из бокового меню, когда опустела; открытый композер этого
        # черновика надо закрыть — иначе он продолжает править то, чего уже нет, и
        # набранное после удаления пропадает молча.
        resp.headers["HX-Trigger"] = json.dumps(
            {"draftDiscarded": str(draft_id), "countsChanged": True}
        )
    return resp


@router.get("/mail/account/{account_id}/{box}/list", response_class=HTMLResponse)
async def account_mail_list(
    request: Request, account_id: uuid.UUID, box: str, page: int = 0
):
    if box not in ("inbox", "sent"):
        box = "inbox"
    page = max(page, 0)
    async with session_scope() as s:
        ctx = await _list_ctx(
            s, box, page=page, account_id=account_id,
            list_url=f"/mail/account/{account_id}/{box}/list",
        )
    return templates.TemplateResponse(
        request, "_maillist.html",
        {**ctx, "view": box, "p_url": f"/mail/account/{account_id}/{box}"},
    )


@router.get("/mail/thread/{thread_id}/rows", response_class=HTMLResponse)
async def thread_rows(request: Request, thread_id: uuid.UUID, box: str = "inbox"):
    """Письма беседы строками — раскрытая группа в списке."""
    async with session_scope() as s:
        emails = await views.list_mail(
            s, box, thread_ids=[thread_id], limit=THREAD_ROWS_LIMIT
        )
        elabels = await labels_svc.labels_for_emails(s, [e.id for e in emails])
    return templates.TemplateResponse(
        request, "_threadrows.html",
        {"emails": emails, "email_labels": elabels, "view": box},
    )


@router.get("/thread/{thread_id}", response_class=HTMLResponse)
async def thread_pane(request: Request, thread_id: uuid.UUID, back: str = ""):
    async with session_scope() as s:
        messages = await views.get_thread_messages(s, thread_id)
    return templates.TemplateResponse(
        request, "_threadpane.html",
        {"messages": messages, "thread_id": thread_id, "back": back},
    )


@router.get("/email/{email_id}", response_class=HTMLResponse)
async def email_pane(request: Request, email_id: uuid.UUID, back: str = ""):
    """Single-message reading pane (full body) for mail rows, with the thread's
    other messages listed below for quick navigation. `back`, when set (e.g. opened
    from a commitment card), drives a ← Назад button back to that origin."""
    # Читаем и пишем РАЗНЫМИ транзакциями. В режиме WAL повышение читающей транзакции
    # до пишущей даёт «database is locked» мгновенно, минуя busy_timeout: снимок устарел,
    # и ждать SQLite считает бессмысленным. Пометка «прочитано» посреди чтения роняла
    # весь запрос 500-кой — письмо не открывалось, хотя строка в списке уже подсветилась.
    async with session_scope() as s:
        email = await views.get_email(s, email_id)
        if email is None:
            return HTMLResponse('<div class="pane-empty">Письмо не найдено.</div>')
        was_unread = email.read_at is None
        messages = (
            await views.get_thread_messages(s, email.thread_id)
            if email.thread_id else []
        )
    empty_body = "Письмо без текста."
    writes = was_unread
    if writes:
        # Письмо должно открыться, даже если записать не вышло: пометка о прочтении и
        # заказ текста — не то, ради чего человек кликнул.
        try:
            async with session_scope() as s:
                if was_unread:
                    await views.set_read(s, email_id, True)
        except Exception:  # noqa: BLE001 — записи второстепенны, чтение уже состоялось
            log.exception("Не удалось отметить письмо прочитанным / заказать текст")
    resp = templates.TemplateResponse(
        request,
        "_emailpane.html",
        {"email": email, "messages": messages, "back": back,
         "empty_body": empty_body,
         # Приглашение из Exchange приходит разметкой без единого слова — рамка вокруг
         # пустоты выглядит поломкой, а кнопка картинок над ней обещает то, чего нет.
         "html_body": bool(email.body_raw_html) and not htmlview.is_blank(email.body_raw_html),
         "remote_images": htmlview.has_remote_images(email.body_raw_html)},
    )
    resp.headers["HX-Trigger"] = "countsChanged"  # refresh unread badge
    return resp


@router.get("/email/{email_id}/html", response_class=HTMLResponse)
async def email_html(email_id: uuid.UUID, images: int = 0):
    """Sanitized original HTML for the sandboxed iframe in the reading pane."""
    async with session_scope() as s:
        email = await views.get_email(s, email_id)
        # Вложенные картинки — часть письма, а не запрос в интернет: их кнопка не прячет.
        inline = await attachments_svc.inline_images(s, email) if email else {}
    body = htmlview.sanitize_email_html(email.body_raw_html if email else None, inline)
    resp = HTMLResponse(htmlview.html_document(body))
    resp.headers["Content-Security-Policy"] = htmlview.csp_header(bool(images))
    return resp


@router.get("/email/{email_id}/attachments", response_class=HTMLResponse)
async def email_attachments(
    request: Request, email_id: uuid.UUID,
):
    """Lazy attachment list for an open message (fetched from IMAP, cached)."""
    async with session_scope() as s:
        items, status = await attachments_svc.fetch_attachments(s, email_id)
    ctx = {"email_id": email_id, "attachments": items, "status": status}
    return templates.TemplateResponse(request, "_attachments.html", ctx)


@router.get("/attachment/{email_id}/{idx}")
async def download_attachment(email_id: uuid.UUID, idx: int):
    found = attachments_svc.attachment_file(email_id, idx)
    if found is None:
        return Response("Вложение не найдено", status_code=404)
    path, meta = found
    return FileResponse(path, media_type=meta["mime"], filename=meta["name"])


@router.get("/attachment/{email_id}/{idx}/view")
async def view_attachment(email_id: uuid.UUID, idx: int):
    # Inline (Content-Disposition: inline) so the OS browser renders it. The native
    # shell routes /view links to the system default browser (see AppDelegate).
    found = attachments_svc.attachment_file(email_id, idx)
    if found is None:
        return Response("Вложение не найдено", status_code=404)
    path, meta = found
    return FileResponse(
        path, media_type=meta["mime"],
        content_disposition_type="inline",
    )


@router.get("/email/{email_id}/projects", response_class=HTMLResponse)
async def email_projects(request: Request, email_id: uuid.UUID):
    """Manual-assign checkboxes for the open email's thread."""
    async with session_scope() as s:
        email = await views.get_email(s, email_id)
        if email is None or email.thread_id is None:
            return HTMLResponse("")
        # Only projects of this email's space can be assigned (isolation).
        space_id = await spaces_svc.space_of_account(s, email.account_id)
        all_projects = await projects_svc.list_projects(s, space_id) if space_id else []
        assigned = {
            p.id for p in await projects_svc.projects_for_thread(s, email.thread_id)
        }
    return templates.TemplateResponse(
        request, "_email_projects.html",
        {"email_id": email_id, "thread_id": email.thread_id,
         "projects": all_projects, "assigned": assigned},
    )


@router.post("/email/{email_id}/read")
async def email_read(email_id: uuid.UUID):
    async with session_scope() as s:
        await views.set_read(s, email_id, True)
    return Response(content="", status_code=200, headers={"HX-Trigger": "countsChanged"})


@router.post("/email/{email_id}/unread")
async def email_unread(email_id: uuid.UUID):
    async with session_scope() as s:
        await views.set_read(s, email_id, False)
    return Response(content="", status_code=200, headers={"HX-Trigger": "countsChanged"})
