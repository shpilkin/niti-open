"""The daily briefing page. The model call runs with no DB session open (it is slow and
needs nothing from the DB), like the draft route."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from email_reader.api.common import current_space, shell_ctx, templates
from email_reader.db import session_scope
from email_reader.memory import briefing as briefing_svc

router = APIRouter()


async def _build(request: Request, force: bool):
    async with session_scope() as s:
        space = await current_space(s, request)
        prep = await briefing_svc.prepare(s, space.id, force=force)
    text = None if prep.cached is not None else await briefing_svc.ask(prep)
    async with session_scope() as s:
        result = prep.cached or await briefing_svc.finish(s, prep, text)
        items = await briefing_svc.hydrate(s, result)
    return result, items


@router.get("/briefing", response_class=HTMLResponse)
async def briefing_page(request: Request):
    result, items = await _build(request, force=False)
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
    return templates.TemplateResponse(
        request, "briefing.html",
        {**shell, "view": "briefing", "title_override": "Брифинг",
         "active_key": "briefing", "briefing": result, "items": items,
         "day_text": f"{result.day:%d.%m.%Y}"},
    )


@router.post("/briefing/refresh")
async def briefing_refresh(request: Request):
    await _build(request, force=True)
    return RedirectResponse("/briefing", status_code=303)
