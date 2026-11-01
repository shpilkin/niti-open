"""Projects: CRUD, per-project mail view, export, manual thread assignment."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from email_reader import attention as attention_svc
from email_reader import labels as labels_svc
from email_reader import models, project_folder, views
from email_reader import projects as projects_svc
from email_reader import spaces as spaces_svc
from email_reader.api.common import current_space, shell_ctx, templates
from email_reader.api.routers.mail import email_projects
from email_reader.db import session_scope
from email_reader.enums import ActionStatus, ActionType

router = APIRouter()

# --- Projects ---


@router.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request):
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
        projects_root = await project_folder.get_projects_root(s)
        threads = await attention_svc.for_threads(s, shell["space"].id, include_quiet=True)
        proposed = await projects_svc.list_proposed(s, shell["space"].id)
    # Секции по состоянию, в порядке срочности; пустые секции не показываются.
    sections = [
        (attention_svc.LABELS[state], [t for t in threads if t.state == state])
        for state in attention_svc.STATES
    ]
    return templates.TemplateResponse(
        request, "projects.html",
        {**shell, "view": "projects", "active_key": "projects",
         "projects_root": projects_root, "title_override": "Нити",
         "proposed": proposed,
         "sections": [(label, rows) for label, rows in sections if rows]},
    )


@router.post("/project/{project_id}/verdict")
async def project_verdict(project_id: uuid.UUID, verdict: str = Form(...)):
    """«Да» / «Нет» на предложение агента (docs/feature-topics.md §9.1 п. 1)."""
    async with session_scope() as s:
        project = await projects_svc.get_project(s, project_id)
        if project is not None and project.status == projects_svc.PROPOSED:
            if verdict == "yes":
                await projects_svc.accept(s, project)
            elif verdict == "no":
                await projects_svc.reject(s, project)
    return RedirectResponse("/projects", status_code=303)


@router.get("/project/{project_id}", response_class=HTMLResponse)
async def project_view(request: Request, project_id: uuid.UUID):
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
        project = await projects_svc.get_project(s, project_id)
        if project is None:
            return RedirectResponse("/projects", status_code=303)
        emails = await views.list_project_mail(s, project_id)
        elabels = await labels_svc.labels_for_emails(s, [e.id for e in emails])
        meetings = await projects_svc.meetings_for_project(s, project_id)
        folder = await project_folder.project_dir(s, project)
        attention = await attention_svc.for_thread(s, project)
        cards = await attention_svc.cards_of(s, project)
        decisions = list((await s.execute(
            select(models.ActionItem)
            .where(projects_svc.card_member(project_id),
                   models.ActionItem.type == str(ActionType.DECISION),
                   models.ActionItem.status != str(ActionStatus.DISMISSED))
            .order_by(models.ActionItem.created_at.desc()).limit(8)
        )).scalars().all())
        digests = {
            pm.meeting_id: pm.digest for pm in (await s.execute(
                select(models.ProjectMeeting)
                .where(models.ProjectMeeting.project_id == project_id)
            )).scalars() if pm.digest
        }
        n_threads = (await s.execute(
            select(func.count(models.ProjectThread.id))
            .where(models.ProjectThread.project_id == project_id)
        )).scalar_one()
    return templates.TemplateResponse(
        request, "project.html",
        {**shell, "view": "inbox", "project": project, "emails": emails,
         "email_labels": elabels, "active_key": f"project:{project_id}",
         "meetings": meetings, "folder": str(folder) if folder else "",
         "attention": attention, "title_override": project.name,
         "on_me": [(i, st) for i, st in cards if st.on_me],
         "theirs": [(i, st) for i, st in cards if st.side == attention_svc.THEIRS],
         "mine": [(i, st) for i, st in cards
                  if st.side == attention_svc.MINE and not st.on_me],
         "decisions": decisions, "digests": digests, "n_threads": n_threads,
         "remind_state": attention_svc.REMIND},
    )


@router.post("/project/{project_id}/keep")
async def project_keep(project_id: uuid.UUID):
    """«Продолжать» на затухшей или отложенной Нити (docs/feature-topics.md §8.4)."""
    async with session_scope() as s:
        project = await projects_svc.get_project(s, project_id)
        if project is not None:
            await attention_svc.keep(s, project)
    return RedirectResponse(f"/project/{project_id}", status_code=303)


@router.post("/project/{project_id}/snooze")
async def project_snooze(project_id: uuid.UUID, until: str = Form("")):
    """«Отложить до…» — одна дата на Нить; без даты ничего не меняется."""
    try:
        day = date.fromisoformat(until.strip())
    except ValueError:
        day = None
    async with session_scope() as s:
        project = await projects_svc.get_project(s, project_id)
        if project is not None and day is not None:
            await attention_svc.snooze(s, project, day)
    return RedirectResponse(f"/project/{project_id}", status_code=303)


@router.post("/project/{project_id}/dismiss-open")
async def project_dismiss_open(project_id: uuid.UUID):
    """«Снять с досок»: обязательства Нити снимаются, не закрываются; отмена — reopen."""
    async with session_scope() as s:
        project = await projects_svc.get_project(s, project_id)
        if project is not None:
            await attention_svc.dismiss_open(s, project)
    return RedirectResponse(f"/project/{project_id}", status_code=303)


@router.post("/project/from-thread", response_class=HTMLResponse)
async def project_from_thread(
    request: Request, thread_id: str = Form(...), email_id: str = Form(...),
):
    """«Сделать нитью» из панели письма: Нить по теме беседы, беседа привязана рукой."""
    async with session_scope() as s:
        email = await views.get_email(s, uuid.UUID(email_id))
        space_id = await spaces_svc.space_of_account(s, email.account_id) if email else None
        if space_id is not None:
            await projects_svc.create_from_thread(
                s, space_id=space_id, thread_id=uuid.UUID(thread_id)
            )
    return await email_projects(request, uuid.UUID(email_id))


async def _card_threads_ctx(s, item_id: uuid.UUID) -> dict:
    item = await views.get_item(s, item_id)
    if item is None:
        return {"item": None}
    all_projects = await projects_svc.list_projects(s, item.space_id)
    inherited: set[str] = set()
    if item.project_id is None:
        inherited = set((await views.projects_for_items(s, [item])).get(item.id, []))
    return {"item": item, "projects": all_projects, "inherited": inherited}


@router.get("/item/{item_id}/threads", response_class=HTMLResponse)
async def item_threads(request: Request, item_id: uuid.UUID):
    """Чипы Нитей в панели обязательства: прямая привязка сильнее унаследованной от
    беседы или встречи (docs/feature-topics.md §8.3)."""
    async with session_scope() as s:
        ctx = await _card_threads_ctx(s, item_id)
    return templates.TemplateResponse(request, "_card_threads.html", ctx)


@router.post("/item/{item_id}/thread", response_class=HTMLResponse)
async def item_thread_set(
    request: Request, item_id: uuid.UUID, project_id: str = Form(""),
):
    """Прямая привязка карточки к Нити; пустой project_id снимает её."""
    async with session_scope() as s:
        item = await views.get_item(s, item_id)
        if item is not None:
            pid = uuid.UUID(project_id) if project_id.strip() else None
            if pid is not None:
                project = await projects_svc.get_project(s, pid)
                if project is None or project.space_id != item.space_id:
                    pid = None
            item.project_id = pid
            s.add(views._journal(item.id, action="thread", project=str(pid) if pid else ""))
        ctx = await _card_threads_ctx(s, item_id)
    return templates.TemplateResponse(request, "_card_threads.html", ctx)


@router.post("/item/{item_id}/thread-new", response_class=HTMLResponse)
async def item_thread_new(request: Request, item_id: uuid.UUID):
    async with session_scope() as s:
        item = await views.get_item(s, item_id)
        if item is not None:
            await projects_svc.create_from_card(s, item)
        ctx = await _card_threads_ctx(s, item_id)
    return templates.TemplateResponse(request, "_card_threads.html", ctx)


@router.get("/project/{project_id}/edit", response_class=HTMLResponse)
async def project_edit_page(request: Request, project_id: uuid.UUID):
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
        project = await projects_svc.get_project(s, project_id)
        if project is None:
            return RedirectResponse("/projects", status_code=303)
        space = await spaces_svc.get_space(s, project.space_id)
    return templates.TemplateResponse(
        request, "project_edit.html",
        {**shell, "view": "projects", "active_key": "projects", "project": project,
         "space": space, "title_override": "Изменить проект"},
    )


def _aliases(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


@router.post("/project")
async def project_create(
    request: Request,
    name: str = Form(...), folder_path: str = Form(""), prompt: str = Form(""),
    include_meetings: str = Form(""), aliases: str = Form(""),
):
    """A new project belongs to the open space — there is no other place it could go."""
    async with session_scope() as s:
        space = await current_space(s, request)
        project = await projects_svc.create_project(
            s, name=name, folder_path=folder_path, prompt=prompt, space_id=space.id,
            include_meetings=bool(include_meetings), aliases=_aliases(aliases),
        )
        await projects_svc.enqueue_backfill(s, 90)
        pid = project.id
    return RedirectResponse(f"/project/{pid}", status_code=303)


@router.post("/project/{project_id}")
async def project_update(
    project_id: uuid.UUID,
    name: str = Form(...), folder_path: str = Form(""), prompt: str = Form(""),
    include_meetings: str = Form(""), aliases: str = Form(""),
):
    async with session_scope() as s:
        await projects_svc.update_project(
            s, project_id, name=name, folder_path=folder_path, prompt=prompt,
            include_meetings=bool(include_meetings), aliases=_aliases(aliases),
        )
        await projects_svc.enqueue_backfill(s, 90)  # prompt changed -> re-classify
    return RedirectResponse(f"/project/{project_id}", status_code=303)


@router.post("/project/{project_id}/delete")
async def project_delete(project_id: uuid.UUID):
    async with session_scope() as s:
        await projects_svc.delete_project(s, project_id)
    return RedirectResponse("/projects", status_code=303)


@router.post("/project/{project_id}/export", response_class=HTMLResponse)
async def project_export(project_id: uuid.UUID):
    """Manually (re)export all assigned threads to the project folder. Additive:
    writes/refreshes files, never deletes anything already there."""
    async with session_scope() as s:
        project = await projects_svc.get_project(s, project_id)
        if project is None:
            return HTMLResponse("Проект не найден", status_code=404)
        n = await projects_svc.export_all_threads_for_project(s, project)
    return HTMLResponse(f"<span class='export-ok'>✓ Выгружено бесед: {n}</span>")


@router.post("/project/{project_id}/assign", response_class=HTMLResponse)
async def project_assign(
    request: Request, project_id: uuid.UUID,
    thread_id: str = Form(...), email_id: str = Form(...),
):
    tid = uuid.UUID(thread_id)
    async with session_scope() as s:
        await projects_svc.assign(s, project_id, tid, source="manual")
        project = await projects_svc.get_project(s, project_id)
        thread = (
            await s.execute(select(models.Thread).where(models.Thread.id == tid))
        ).scalar_one_or_none()
        if project and thread:
            await projects_svc.export_thread_to_project(s, project, thread)
    return await email_projects(request, uuid.UUID(email_id))


@router.post("/project/{project_id}/unassign", response_class=HTMLResponse)
async def project_unassign(
    request: Request, project_id: uuid.UUID,
    thread_id: str = Form(...), email_id: str = Form(...),
):
    async with session_scope() as s:
        await projects_svc.unassign(s, project_id, uuid.UUID(thread_id))
    return await email_projects(request, uuid.UUID(email_id))
