"""Meetings: the list of recorded conversations, their reading pane, and manual routing."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import select

from email_reader import meetings as meetings_svc
from email_reader import projects as projects_svc
from email_reader import spaces as spaces_svc
from email_reader.api.common import PAGE_SIZE, current_space, shell_ctx, templates
from email_reader.attachments import _safe_name
from email_reader.db import session_scope
from email_reader.models import ActionItem

router = APIRouter()

# Transcript shown in the pane. Long enough for a two-hour meeting to be readable,
# bounded so a runaway file can't freeze the window.
_PANE_TRANSCRIPT_CHARS = 60_000


def _spaces_json(spaces) -> str:
    """The spaces for the client-side menus (row context menu, the pane's space button)."""
    return json.dumps([{"id": str(sp.id), "name": sp.name} for sp in spaces], ensure_ascii=False)


async def _list_ctx(s, page: int, unrouted: bool, space_id: uuid.UUID) -> dict:
    rows = await meetings_svc.list_meetings(
        s, limit=PAGE_SIZE, offset=page * PAGE_SIZE, unrouted_only=unrouted, space_id=space_id
    )
    total = await meetings_svc.count_meetings(s, unrouted_only=unrouted, space_id=space_id)
    projects = await projects_svc.list_projects(s, space_id)
    # Показываем состояние ДО нажатия: сколько не разобрано и, если разбор вообще
    # невозможен, почему. Кнопка, которая ставит в очередь заведомо обречённую работу,
    # хуже её отсутствия — пользователь жмёт и не понимает, почему ничего не меняется.
    stuck = await meetings_svc.unprocessed_meetings(s)
    parked, parked_reason = await meetings_svc.parked_meetings(s)
    spaces = await spaces_svc.list_spaces(s)
    return {
        "view": "meetings",
        "space_id": space_id,
        "spaces_json": _spaces_json(spaces),
        "unprocessed": len(stuck),
        "routing_blocker": await meetings_svc.routing_blocker(s) if stuck else None,
        # Отказ провайдера — не настройка: кнопку оставляем, но молчать нельзя. Человек
        # жал «Разобрать», работа падала на 402, и на экране не менялось ничего.
        "routing_failure": await meetings_svc.last_routing_failure(s, stuck) if stuck else None,
        "parked": parked, "parked_reason": parked_reason,
        "meetings": rows,
        "memberships": await projects_svc.meeting_memberships(s, [m.id for m in rows]),
        "project_names": {p.id: p.name for p in projects},
        "unrouted": unrouted,
        "p_page": page, "p_total": total, "p_size": PAGE_SIZE,
        "p_target": "#list", "p_url": "/meetings" + ("?filter=unrouted" if unrouted else ""),
    }


@router.get("/meetings", response_class=HTMLResponse)
async def meetings_page(request: Request, page: int = 0, filter: str = ""):
    page = max(page, 0)
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
        ctx = {**shell, **await _list_ctx(s, page, filter == "unrouted", shell["space"].id)}
    ctx["active_key"] = "meetings"
    return templates.TemplateResponse(request, "meetings.html", ctx)


@router.get("/meetings/list", response_class=HTMLResponse)
async def meetings_list(request: Request, page: int = 0, filter: str = ""):
    """The list alone (filter chips, pager, post-scan refresh)."""
    async with session_scope() as s:
        space = await current_space(s, request)
        ctx = await _list_ctx(s, max(page, 0), filter == "unrouted", space.id)
    return templates.TemplateResponse(request, "_meetinglist.html", ctx)


# Заголовок в имени файла режем коротко: у неразобранной встречи title — это первая
# фраза расшифровки целиком, и без обрезки в «Загрузках» оказывается строка на полсотни
# слов. Дата впереди всё равно делает имя различимым.
_NAME_TITLE_CHARS = 50


def _download_name(meeting) -> str:
    """«2026-06-21 15-16 Планёрка по крабам.txt» — по имени должно быть видно, что это
    за запись, без открытия."""
    when = meeting.started_at.astimezone().strftime("%Y-%m-%d %H-%M") if meeting.started_at else ""
    title = _safe_name(meeting.title or "встреча")[:_NAME_TITLE_CHARS].strip(" .,-")
    return f"{when} {title}".strip() + ".txt"


async def _pane_ctx(
    s, meeting_id: uuid.UUID, at: str = "", current_space_id: uuid.UUID | None = None
) -> dict:
    meeting = await meetings_svc.get_meeting(s, meeting_id)
    if meeting is None:
        return {"meeting": None}
    spaces = await spaces_svc.list_spaces(s)
    members = await projects_svc.projects_for_meeting(s, meeting_id)
    member_ids = {p.id for p in members}
    items = list(
        (
            await s.execute(
                select(ActionItem)
                .where(ActionItem.source_meeting_id == meeting_id)
                .order_by(ActionItem.created_at)
            )
        ).scalars().all()
    )
    candidates = [
        p for p in await projects_svc.list_for_meetings(s, meeting.space_id)
        if p.id not in member_ids
    ]
    audio = await meetings_svc.resolve(s, meeting.audio_path)
    transcript = await meetings_svc.read_transcript(
        s, meeting, max_chars=_PANE_TRANSCRIPT_CHARS
    )
    span = meetings_svc.timecode_span(transcript or "", at)
    return {
        "meeting": meeting,
        "projects": members,
        "candidates": candidates,
        "items": items,
        "suggestions": [
            s_ for s_ in (meeting.suggestions or [])
            if uuid.UUID(s_["project_id"]) not in member_ids
        ],
        "transcript": transcript,
        # Opened from a task said at a meeting: the moment it quotes, so the reader lands
        # on it instead of on 40 000 characters from the start.
        "hit": (
            {
                "before": transcript[: span[0]],
                "text": transcript[span[0] : span[1]],
                "after": transcript[span[1] :],
            }
            if span
            else None
        ),
        "audio_path": str(audio) if audio else None,
        # The pane offers to move the meeting when there is somewhere else to move it;
        # opened from another space's screen it says where the meeting now lives.
        "spaces": spaces,
        "spaces_json": _spaces_json(spaces),
        "space_name": next((sp.name for sp in spaces if sp.id == meeting.space_id), ""),
        "current_space_id": current_space_id,
    }


@router.get("/meeting/{meeting_id}", response_class=HTMLResponse)
async def meeting_pane(request: Request, meeting_id: uuid.UUID, at: str = ""):
    async with session_scope() as s:
        space = await current_space(s, request)
        ctx = await _pane_ctx(s, meeting_id, at, space.id)
    return templates.TemplateResponse(request, "_meetingpane.html", ctx)


async def _action_ctx(
    s, space, meeting_id: uuid.UUID, unrouted: bool, pane_only: bool = False
) -> dict:
    """After move / delete / restore: this space's list plus the pane, rendered together
    (_meetingaction.html swaps the pane out of band) — one action, both places update.

    pane_only: the pane was opened from a task on a board or a project page, where the
    meetings list is not on screen. Then only the pane comes back, and the meetingChanged
    trigger makes that page refetch its own rows (ui.js) — they just changed too."""
    pane = await _pane_ctx(s, meeting_id, "", space.id)
    if pane_only:
        return pane
    return {**await _list_ctx(s, 0, unrouted, space.id), **pane}


def _action_response(request: Request, ctx: dict, trigger: dict, pane_only: bool = False):
    name = "_meetingpane.html" if pane_only else "_meetingaction.html"
    resp = templates.TemplateResponse(request, name, ctx)
    resp.headers["HX-Trigger"] = json.dumps(trigger)
    return resp


def _changed(meeting_id: uuid.UUID, **more) -> dict:
    """HX-Trigger of a meeting action: the sidebar counts, and the rows under the pane on
    pages without the meetings list (ui.js refetches them on meetingChanged)."""
    return {"countsChanged": True, "meetingChanged": {"id": str(meeting_id)}, **more}


@router.get("/meeting/{meeting_id}/download")
async def meeting_transcript_download(meeting_id: uuid.UUID):
    """The transcript as a .txt file.

    Served from the archive, not from the database: Niti never stores a copy of the
    recording's text (§4.4), it keeps the path. So a moved archive means an honest 404
    rather than a stale copy.

    The path ends in «/download» on purpose — the macOS shell decides what to save from
    the URL (AppDelegate.decidePolicyFor navigationResponse), and text/plain is something
    the webview would happily render over the whole app instead."""
    async with session_scope() as s:
        meeting = await meetings_svc.get_meeting(s, meeting_id)
        if meeting is None:
            return Response("Встреча не найдена", status_code=404)
        path = await meetings_svc.resolve(s, meeting.transcript_path)
        if path is None or not path.is_file():
            return Response(
                "Файл расшифровки не найден — проверьте, где лежит архив записей.",
                status_code=404,
            )
        name = _download_name(meeting)
    return FileResponse(
        path, media_type="text/plain; charset=utf-8", filename=name,
    )


@router.post("/meeting/{meeting_id}/space", response_class=HTMLResponse)
async def meeting_move(
    request: Request, meeting_id: uuid.UUID, space_id: str = Form(...),
    filter: str = Form(""), pane_only: str = Form(""),
):
    """Move the meeting and everything extracted from it to another space. The meeting
    leaves this space's list; the pane says where it went and offers to go there."""
    async with session_scope() as s:
        space = await current_space(s, request)
        try:
            await meetings_svc.set_space(s, meeting_id, uuid.UUID(space_id))
        except ValueError:
            pass
        ctx = await _action_ctx(s, space, meeting_id, filter == "unrouted", bool(pane_only))
    return _action_response(request, ctx, _changed(meeting_id), bool(pane_only))


@router.post("/meeting/{meeting_id}/delete", response_class=HTMLResponse)
async def meeting_delete(
    request: Request, meeting_id: uuid.UUID, filter: str = Form(""), pane_only: str = Form("")
):
    """Soft delete with an undo toast (the app's idiom for resolving actions): no modal,
    the meeting is back in one click for a few seconds, and stays restorable from the
    pane of any task it produced."""
    async with session_scope() as s:
        space = await current_space(s, request)
        meeting = await meetings_svc.delete_meeting(s, meeting_id)
        ctx = await _action_ctx(s, space, meeting_id, filter == "unrouted", bool(pane_only))
    title = (meeting.title if meeting else None) or "без названия"
    undo = {
        "url": f"/meeting/{meeting_id}/restore", "label": f"Встреча удалена: «{title}»",
        "values": {"filter": filter, "pane_only": pane_only},
    }
    if pane_only:
        undo["target"] = "#pane"
    return _action_response(request, ctx, _changed(meeting_id, showUndo=undo), bool(pane_only))


@router.post("/meeting/{meeting_id}/restore", response_class=HTMLResponse)
async def meeting_restore(
    request: Request, meeting_id: uuid.UUID, filter: str = Form(""), pane_only: str = Form("")
):
    async with session_scope() as s:
        space = await current_space(s, request)
        await meetings_svc.restore_meeting(s, meeting_id)
        ctx = await _action_ctx(s, space, meeting_id, filter == "unrouted", bool(pane_only))
    return _action_response(
        request, ctx, _changed(meeting_id, showToast="Встреча восстановлена"), bool(pane_only)
    )


@router.post("/meeting/{meeting_id}/assign", response_class=HTMLResponse)
async def meeting_assign(
    request: Request, meeting_id: uuid.UUID, project_id: str = Form(...)
):
    """File the meeting into a project by hand — from a suggestion or from scratch."""
    async with session_scope() as s:
        meeting = await meetings_svc.get_meeting(s, meeting_id)
        if meeting is None:
            return HTMLResponse("Встреча не найдена", status_code=404)
        await projects_svc.assign_meeting(s, uuid.UUID(project_id), meeting_id,
                                          source="manual", reason="выбрано вручную")
        # A confirmed suggestion stops being a suggestion.
        meeting.suggestions = [
            x for x in (meeting.suggestions or []) if x.get("project_id") != project_id
        ]
        await s.flush()
        await projects_svc.export_meeting(s, meeting)
        ctx = await _pane_ctx(s, meeting_id)
    return templates.TemplateResponse(request, "_meetingpane.html", ctx)


@router.post("/meeting/{meeting_id}/unassign", response_class=HTMLResponse)
async def meeting_unassign(
    request: Request, meeting_id: uuid.UUID, project_id: str = Form(...)
):
    async with session_scope() as s:
        meeting = await meetings_svc.get_meeting(s, meeting_id)
        if meeting is None:
            return HTMLResponse("Встреча не найдена", status_code=404)
        pid = uuid.UUID(project_id)
        await projects_svc.unassign_meeting(s, pid, meeting_id)
        await s.flush()
        # The note in that folder is flagged, not deleted (§9.3).
        await projects_svc.export_meeting(s, meeting, removed={pid})
        ctx = await _pane_ctx(s, meeting_id)
    return templates.TemplateResponse(request, "_meetingpane.html", ctx)


@router.post("/meeting/{meeting_id}/thread-new", response_class=HTMLResponse)
async def meeting_thread_new(request: Request, meeting_id: uuid.UUID):
    """«Сделать нитью» из панели встречи: Нить по её теме, встреча привязана рукой."""
    async with session_scope() as s:
        meeting = await meetings_svc.get_meeting(s, meeting_id)
        if meeting is None:
            return HTMLResponse("Встреча не найдена", status_code=404)
        await projects_svc.create_from_meeting(s, meeting)
        await s.flush()
        await projects_svc.export_meeting(s, meeting)
        ctx = await _pane_ctx(s, meeting_id)
    return templates.TemplateResponse(request, "_meetingpane.html", ctx)


@router.post("/meetings/process", response_class=HTMLResponse)
async def meetings_process(request: Request, filter: str = ""):
    """Re-run routing for meetings that nothing else will pick up (see
    meetings.requeue_unprocessed). Queues only — the worker does the work."""
    async with session_scope() as s:
        space = await current_space(s, request)
        blocked = await meetings_svc.routing_blocker(s)
        queued = 0 if blocked else await meetings_svc.requeue_unprocessed(s)
        ctx = await _list_ctx(s, 0, filter == "unrouted", space.id)
    ctx["process_stats"] = {"queued": queued, "blocked": blocked}
    return templates.TemplateResponse(request, "_meetinglist.html", ctx)


@router.post("/meetings/scan", response_class=HTMLResponse)
async def meetings_scan(request: Request, filter: str = ""):
    """Sweep the handover folder now instead of waiting for the next tick."""
    stats = await meetings_svc.scan_inbox()
    async with session_scope() as s:
        space = await current_space(s, request)
        ctx = await _list_ctx(s, 0, filter == "unrouted", space.id)
    ctx["scan_stats"] = stats
    resp = templates.TemplateResponse(request, "_meetinglist.html", ctx)
    resp.headers["HX-Trigger"] = "countsChanged"
    return resp
