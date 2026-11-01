"""Composer: new mail, reply/forward (single message and whole-thread), send."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from email_reader import compose as compose_svc
from email_reader import contacts as contacts_svc
from email_reader import maildrafts as maildrafts_svc
from email_reader import signatures as signatures_svc
from email_reader.api.common import current_space, templates
from email_reader.db import session_scope

router = APIRouter()


def _uuid_or_none(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        return None


@router.get("/compose/recipients", response_class=HTMLResponse)
async def recipient_suggestions(request: Request, q: str = ""):
    """Подсказка адресов при вводе получателя — только те, у кого почта есть."""
    async with session_scope() as s:
        space = await current_space(s, request)
        found = await contacts_svc.suggest(s, q, space_id=space.id)
    return templates.TemplateResponse(
        request, "_recipients.html", {"suggestions": found}
    )


@router.get("/compose", response_class=HTMLResponse)
async def compose_new(
    request: Request, to: str = "", subject: str = "", body: str = "",
    account_id: str = "", draft_id: str = "",
):
    """A blank letter, one with the fields already filled — that is the link the MCP tool
    `compose_link` hands an agent, so the person presses «Отправить» themselves — or a
    draft picked back up."""
    back_url, prefill = "", None
    async with session_scope() as s:
        accs = await compose_svc.enabled_accounts(s, (await current_space(s, request)).id)
        sigs = await signatures_svc.for_accounts(s, accs)
        sig_texts = signatures_svc.to_texts(sigs)
        sig_fonts = signatures_svc.to_fonts(sigs)
        draft = await maildrafts_svc.get(s, draft_id) if draft_id else None
        if draft is not None:
            # Текст черновика идёт как есть: подпись в нём уже своя, и добавить её
            # второй раз значило бы отправить письмо с двумя подписями.
            prefill = {
                "to": draft.to_line, "cc": draft.cc_line, "subject": draft.subject,
                "body": draft.body, "draft": "", "quoted": "",
                "in_reply_to": draft.in_reply_to or None,
                "references": (draft.references or "").split(),
                "forward_email_id": draft.forward_email_id,
                "account_id": draft.account_id, "id": draft.id,
            }
            back_url = draft.back_url
    if prefill is None:
        from_acc = _uuid_or_none(account_id)
        if to or subject or body or from_acc is not None:
            prefill = {"to": to, "cc": "", "subject": subject, "draft": body,
                       "quoted": "", "in_reply_to": None, "references": [],
                       "forward_email_id": None, "account_id": from_acc}
    return templates.TemplateResponse(
        request, "_compose.html",
        {"accounts": accs, "ctx": prefill, "back_url": back_url, "signatures": sigs,
         "sig_texts": sig_texts, "sig_fonts": sig_fonts},
    )


@router.post("/drafts/save")
async def draft_save(
    account_id: str = Form(...),
    draft_id: str = Form(""),
    to: str = Form(""),
    cc: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    in_reply_to: str = Form(""),
    references: str = Form(""),
    forward_email_id: str = Form(""),
    back_url: str = Form(""),
):
    """Автосохранение композера. Отвечает `id` черновика, чтобы следующее сохранение
    правило ту же строку, а не плодило новые."""
    acc = _uuid_or_none(account_id)
    if acc is None:
        return JSONResponse({"draft_id": None})
    async with session_scope() as s:
        draft = await maildrafts_svc.save(
            s, draft_id=draft_id, account_id=acc, to=to, cc=cc, subject=subject,
            body=body, in_reply_to=in_reply_to, references=references,
            forward_email_id=forward_email_id, back_url=back_url,
        )
        saved = str(draft.id) if draft is not None else None
    # Первое сохранение заводит папку «Черновики» в боковом меню — её надо перерисовать.
    return JSONResponse({"draft_id": saved, "created": bool(saved and not draft_id)})


@router.get("/reply/{email_id}", response_class=HTMLResponse)
async def reply(request: Request, email_id: uuid.UUID, back: str = "", all: int = 0):
    async with session_scope() as s:
        ctx = await compose_svc.reply_context(s, email_id, all_=bool(all))
        accs = await compose_svc.enabled_accounts(s, (await current_space(s, request)).id)
        sigs = await signatures_svc.for_accounts(s, accs)
        sig_texts = signatures_svc.to_texts(sigs)
        sig_fonts = signatures_svc.to_fonts(sigs)
    return templates.TemplateResponse(
        request, "_compose.html",
        {"accounts": accs, "ctx": ctx, "back_url": back or f"/email/{email_id}",
         "signatures": sigs, "sig_texts": sig_texts, "sig_fonts": sig_fonts},
    )


@router.get("/reply_thread/{thread_id}", response_class=HTMLResponse)
async def reply_thread(request: Request, thread_id: uuid.UUID, back: str = "", all: int = 0):
    async with session_scope() as s:
        ctx = await compose_svc.reply_context_thread(s, thread_id, all_=bool(all))
        accs = await compose_svc.enabled_accounts(s, (await current_space(s, request)).id)
        sigs = await signatures_svc.for_accounts(s, accs)
        sig_texts = signatures_svc.to_texts(sigs)
        sig_fonts = signatures_svc.to_fonts(sigs)
    return templates.TemplateResponse(
        request, "_compose.html",
        {"accounts": accs, "ctx": ctx, "back_url": back or f"/thread/{thread_id}",
         "signatures": sigs, "sig_texts": sig_texts, "sig_fonts": sig_fonts},
    )


@router.get("/forward/{email_id}", response_class=HTMLResponse)
async def forward(request: Request, email_id: uuid.UUID, back: str = ""):
    async with session_scope() as s:
        ctx = await compose_svc.forward_context(s, email_id)
        accs = await compose_svc.enabled_accounts(s, (await current_space(s, request)).id)
        sigs = await signatures_svc.for_accounts(s, accs)
        sig_texts = signatures_svc.to_texts(sigs)
        sig_fonts = signatures_svc.to_fonts(sigs)
    return templates.TemplateResponse(
        request, "_compose.html",
        {"accounts": accs, "ctx": ctx, "back_url": back or f"/email/{email_id}",
         "signatures": sigs, "sig_texts": sig_texts, "sig_fonts": sig_fonts},
    )


@router.get("/forward_thread/{thread_id}", response_class=HTMLResponse)
async def forward_thread(request: Request, thread_id: uuid.UUID, back: str = ""):
    async with session_scope() as s:
        ctx = await compose_svc.forward_context_thread(s, thread_id)
        accs = await compose_svc.enabled_accounts(s, (await current_space(s, request)).id)
        sigs = await signatures_svc.for_accounts(s, accs)
        sig_texts = signatures_svc.to_texts(sigs)
        sig_fonts = signatures_svc.to_fonts(sigs)
    return templates.TemplateResponse(
        request, "_compose.html",
        {"accounts": accs, "ctx": ctx, "back_url": back or f"/thread/{thread_id}",
         "signatures": sigs, "sig_texts": sig_texts, "sig_fonts": sig_fonts},
    )


@router.post("/send", response_class=HTMLResponse)
async def send(
    request: Request,
    account_id: str = Form(...),
    to: str = Form(...),
    cc: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    in_reply_to: str = Form(""),
    references: str = Form(""),
    back_url: str = Form(""),
    forward_email_id: str = Form(""),
    draft_id: str = Form(""),
    attachments: list[UploadFile] = File(default=[]),
):
    refs = references.split() if references else []
    files: list[dict] = []
    for f in attachments:
        data = await f.read()
        if not data:  # browsers send an empty part when no file is picked
            continue
        files.append({
            "name": f.filename or "attachment",
            "mime": f.content_type or "application/octet-stream",
            "bytes": data,
        })
    async with session_scope() as s:
        result = await compose_svc.send(
            s, account_id=uuid.UUID(account_id), to=to, subject=subject, body=body,
            in_reply_to=in_reply_to or None, references=refs,
            forward_email_id=uuid.UUID(forward_email_id) if forward_email_id else None,
            attachments=files, cc=cc,
        )
    if result.error:
        # Черновик остаётся: иначе неудачная отправка стирала бы набранный текст.
        return templates.TemplateResponse(
            request, "_send_result.html", {"error": result.error, "to": to}
        )
    if draft_id:
        async with session_scope() as s:
            await maildrafts_svc.discard(s, draft_id)
    # Success: close the composer (no swap), let the client show a toast and
    # navigate back to where the user was. Prevents re-sending the same draft.
    trigger = json.dumps({"composeSent": {
        "to": to, "back": back_url or "",
    }})
    return Response(content="", status_code=200, headers={"HX-Trigger": trigger})
