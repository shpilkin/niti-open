"""Settings (all sections), the sidebar and queue-status partials, and global actions."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape

from email_reader import accounts as accounts_svc
from email_reader import ai_providers as ai_providers_mod
from email_reader import (
    app_prefs,
    maintenance,
    project_folder,
    settings_sections,
    settings_store,
    views,
)
from email_reader import boards as boards_svc
from email_reader import identities as identities_svc
from email_reader import labels as labels_svc
from email_reader import meetings as meetings_svc
from email_reader import model_connections as mc_svc
from email_reader import profiles as profiles_svc
from email_reader import projects as projects_svc
from email_reader import prompts as prompts_svc
from email_reader import providers as providers_mod
from email_reader import signatures as signatures_svc
from email_reader import spaces as spaces_svc
from email_reader import vip as vip_svc
from email_reader.ai import extractor as ai_extractor
from email_reader.api.common import (
    _agent_status_ctx,
    current_space,
    shell_ctx,
    templates,
)
from email_reader.config import get_app_config, get_settings
from email_reader.db import session_scope
from email_reader.enums import ACTIONABLE_TYPES, TYPE_LABEL
from email_reader.mcp import auth as mcp_auth
from email_reader.memory import capture as capture_svc
from email_reader.memory import facts as facts_svc
from email_reader.models import MemoryFact
from email_reader.search import index as search_index

router = APIRouter()

# Многозначное поле формы (чекбоксы типов у доски). Модульный singleton, а не вызов Form()
# в значении по умолчанию — так требует ruff B008 и так его видно один раз.
_TYPES_FORM = Form(default=[])


@router.get("/ui/sidebar", response_class=HTMLResponse)
async def ui_sidebar(request: Request, active: str = ""):
    """The sidebar is the open space: its boards' counts, its mailboxes, its projects —
    and the switcher to the other spaces."""
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
    return templates.TemplateResponse(
        request, "_sidebar.html", {**shell, "active": active, "view": ""}
    )


async def _spaces_ctx(s) -> dict:
    """Every space with what it contains — the «Пространства» screen and its partial."""
    spaces = await spaces_svc.list_spaces(s)
    conns = await mc_svc.list_connections(s)
    rows = []
    for sp in spaces:
        rows.append({
            "space": sp,
            "accounts": await accounts_svc.list_accounts(s, sp.id),
            "bots": await spaces_svc.list_bots(s, sp.id),
            "delete_blocker": await spaces_svc.delete_blocker(s, sp.id),
        })
    return {"space_rows": rows, "spaces": spaces, "connections": conns,
            "default_model_name": next((c.name for c in conns if c.is_default), None)}


async def _spaces_partial(request: Request):
    async with session_scope() as s:
        ctx = await _spaces_ctx(s)
    return templates.TemplateResponse(request, "_spaces.html", ctx)


def _status(request: Request, ctx: dict, toast: str | None = None):
    """The sidebar's queue-status fragment; an action's outcome rides along as a toast."""
    resp = templates.TemplateResponse(request, "_agent_status.html", ctx)
    if toast:
        resp.headers["HX-Trigger"] = json.dumps({"showToast": toast})
    return resp


@router.get("/ui/agent-status", response_class=HTMLResponse)
async def ui_agent_status(request: Request):
    async with session_scope() as s:
        ctx = await _agent_status_ctx(s)
    return _status(request, ctx)


@router.post("/actions/sync-now", response_class=HTMLResponse)
async def action_sync_now(request: Request):
    import asyncio

    from email_reader import calendar_sync
    from email_reader.config import get_app_config
    from email_reader.connectors.sync_worker import sync_once

    # Fire-and-forget: runs in the shared event loop (same process as the worker).
    # Mail and calendar refresh together — «Синхронизировать» means everything.
    asyncio.create_task(sync_once(get_app_config()))
    asyncio.create_task(calendar_sync.sync_all_accounts(datetime.now(UTC)))
    async with session_scope() as s:
        ctx = await _agent_status_ctx(s)
    return _status(request, ctx, "Синхронизация запущена…")


@router.post("/actions/reprocess", response_class=HTMLResponse)
async def action_reprocess(request: Request):
    from sqlalchemy import text

    async with session_scope() as s:
        # Requeue model events that failed/parked (e.g. model was added later, or an
        # outage). Covers thread analysis, project matching and meeting routing, plus
        # events parked while the model was unavailable.
        await s.execute(
            text(
                "UPDATE events SET status='pending', attempts=0, available_at=:now "
                "WHERE type IN ('ThreadReady','ProjectMatch','MeetingReady') "
                "AND status IN ('failed','stalled','processing')"
            ),
            {"now": datetime.now(UTC)},
        )
        ctx = await _agent_status_ctx(s)
    return _status(request, ctx, "Разбор писем перезапущен…")


@router.get("/settings")
async def settings_root():
    return RedirectResponse(
        f"/settings/section/{settings_sections.DEFAULT_SECTION}", status_code=303
    )


@router.get("/settings/section/{section}", response_class=HTMLResponse)
async def settings_section(request: Request, section: str):
    if section not in settings_sections.ACTIVE_KEYS:
        return RedirectResponse(
            f"/settings/section/{settings_sections.DEFAULT_SECTION}", status_code=303
        )
    ctx: dict = {
        "view": "settings",
        "title_override": "Настройки",
        "sections": settings_sections.SECTIONS,
        "active": section,
    }
    async with session_scope() as s:
        # Раздел настроек кладётся поверх: у «Пространств» свой список ящиков.
        ctx = {**await shell_ctx(s, request), **ctx}
        if section == "spaces":
            ctx.update(await _spaces_ctx(s))
            ctx["providers"] = providers_mod.PROVIDERS
        elif section == "labels":
            ctx["labels"] = await labels_svc.list_labels(s)
            ctx["rules"] = await labels_svc.list_rules(s)
            ctx["vip_text"] = await vip_svc.get_raw(s)
            ctx["vip_placeholder"] = vip_svc.PLACEHOLDER
            ctx["vip_suggestions"] = await vip_svc.suggestions(s)
        elif section == "boards":
            ctx.update(await _boards_ctx(s, request))
        elif section == "identities":
            ctx["identities"] = await identities_svc.list_identities(s)
        elif section == "general":
            try:
                ctx["cfg"] = get_app_config()
            except FileNotFoundError:
                ctx["cfg"] = None
            ctx["extract_backfill_days"] = await app_prefs.get_extract_backfill_days(s)
            ctx["idle_days"] = await app_prefs.get_idle_days(s)
            ctx["remind_days"] = await app_prefs.get_remind_days(s)
            ctx["projects_root"] = await project_folder.get_projects_root(s)
            ctx["mail_grouped"] = await views.mail_grouped(s)
        elif section == "signatures":
            accts = await accounts_svc.list_accounts(s)
            ctx["signature_accounts"] = [
                {"account": a,
                 "value": await signatures_svc.get_signature(s, a.id)}
                for a in accts
            ]
        elif section == "profile":
            ctx["profile_spaces"] = [
                {"space": sp, "value": await profiles_svc.core(s, sp.id),
                 "memory": await capture_svc.screen(s, sp.id)}
                for sp in await spaces_svc.list_spaces(s)
            ]
            ctx["profile_placeholder"] = profiles_svc.PLACEHOLDER
        elif section == "prompts":
            groups = []
            for a in await spaces_svc.list_spaces(s):
                rows = []
                for spec in prompts_svc.PROMPTS:
                    override = await prompts_svc.get_override(s, spec.key, a.id)
                    rows.append({
                        **_prompt_card_ctx(spec, a.id, override or "", override is not None),
                    })
                groups.append({"space": a, "prompts": rows})
            ctx["prompt_spaces"] = groups
        elif section == "meetings":
            chosen = await settings_store.get_value(s, meetings_svc.SPACE_KEY)
            ctx["spaces"] = await spaces_svc.list_spaces(s)
            ctx["meetings_space_id"] = chosen or str(await meetings_svc.routing_space_id(s))
            ctx["connections"] = await mc_svc.list_connections(s)
            ctx["meetings_model_id"] = (
                await settings_store.get_value(s, meetings_svc.MODEL_KEY)
            ) or ""
            ctx["asr_root"] = await meetings_svc.get_asr_root(s)
            ctx["projects_root"] = await project_folder.get_projects_root(s)
            ctx["meeting_projects"] = await projects_svc.list_for_meetings(s)
            ctx["inbox_dir"] = str(meetings_svc.inbox_dir())
        elif section == "storage":
            ctx["stats"] = await maintenance.local_stats(s)
        elif section == "security":
            ctx["mcp_enabled"] = await mcp_auth.is_enabled(s)
            ctx["mcp_has_token"] = await mcp_auth.has_token(s)
            ctx["mcp_url"] = f"http://{get_settings().web_host}:{get_settings().web_port}/mcp"
        elif section == "ai":
            ctx["connections"] = await mc_svc.list_connections(s)
            ctx["ai_providers"] = ai_providers_mod.AI_PROVIDERS
            ctx["llm_usage"] = await maintenance.llm_usage(s)
    # Tab switch (HX-Request) -> return only the settings body so just #list swaps
    # and the sidebar/ПОЧТА stays mounted. A browser Back that htmx restores from
    # history sends HX-History-Restore-Request and needs the full page instead.
    partial = request.headers.get("HX-Request") and not request.headers.get(
        "HX-History-Restore-Request"
    )
    tpl = "_settings_body.html" if partial else "settings.html"
    return templates.TemplateResponse(request, tpl, ctx)


@router.post("/settings/signatures/{account_id}", response_class=HTMLResponse)
async def signature_save(account_id: uuid.UUID, body: str = Form("")):
    async with session_scope() as s:
        await signatures_svc.set_signature(s, account_id, body)
    return HTMLResponse("<span class='export-ok'>✓ Сохранено</span>")


@router.post("/settings/profile/{space_id}", response_class=HTMLResponse)
async def profile_save(space_id: uuid.UUID, body: str = Form("")):
    async with session_scope() as s:
        await profiles_svc.set_profile(s, space_id, body)
    return HTMLResponse("<span class='export-ok'>✓ Сохранено</span>")


async def _memory_block(request: Request, s, space_id: uuid.UUID):  # noqa: ANN001
    """Every action on the memory of a space answers with the whole block: the remembered
    lines, the proposals and the stale list change together."""
    ctx = {"space": await spaces_svc.get_space(s, space_id),
           "memory": await capture_svc.screen(s, space_id)}
    return templates.TemplateResponse(request, "settings/_memory.html", ctx)


@router.post("/settings/profile/{space_id}/forget", response_class=HTMLResponse)
async def profile_forget(request: Request, space_id: uuid.UUID, fact_id: str = Form("")):
    """Retire one remembered line; the memory block comes back re-rendered."""
    async with session_scope() as s:
        try:
            wanted = uuid.UUID(fact_id)
        except ValueError:
            wanted = None
        if wanted is not None:
            await profiles_svc.forget(s, space_id, wanted)
        return await _memory_block(request, s, space_id)


@router.post("/settings/profile/{space_id}/facts/{fact_id}", response_class=HTMLResponse)
async def profile_fact_verdict(
    request: Request, space_id: uuid.UUID, fact_id: uuid.UUID, verdict: str = Form("")
):
    """The owner's word on one fact of this space's screen: «да» (a proposal goes live, a
    live fact is vouched for again), «нет» (a proposal is rejected and remembered as
    such), «забыть» (a live fact is retired). Anything else — the block comes back as is."""
    async with session_scope() as s:
        fact = await s.get(MemoryFact, fact_id)
        ours = fact is not None and (fact.space_id == space_id or fact.captured_in == space_id)
        if ours and verdict == "confirm":
            await capture_svc.confirm(s, fact)
        elif ours and verdict == "reject" and fact.status == facts_svc.PROPOSED:
            await facts_svc.reject(s, fact)
        elif ours and verdict == "forget" and fact.status == facts_svc.ACTIVE:
            await facts_svc.retire(s, fact)
        return await _memory_block(request, s, space_id)


@router.post("/settings/profile/{space_id}/capture", response_class=HTMLResponse)
async def profile_capture(request: Request, space_id: uuid.UUID, action: str = Form("")):
    """The capture switch and its two buttons. `now` queues a pass over what is new since
    the watermark; `backfill` first moves the watermark back and then queues — the pass
    itself runs in the worker, one batch per event, never here."""
    async with session_scope() as s:
        if action in ("on", "off"):
            await capture_svc.set_on(s, space_id, action == "on")
        elif action == "now" and await capture_svc.is_on(s, space_id):
            await capture_svc.request(s, space_id, "now")
        elif action == "backfill" and await capture_svc.is_on(s, space_id):
            await capture_svc.move_back(s, space_id)
            await capture_svc.request(s, space_id, "backfill")
        return await _memory_block(request, s, space_id)


@router.post("/settings/vip", response_class=HTMLResponse)
async def vip_save(body: str = Form("")):
    async with session_scope() as s:
        await vip_svc.set_raw(s, body)
        n = len(await vip_svc.get_entries(s))
    return HTMLResponse(f"<span class='export-ok'>✓ Сохранено ({n})</span>")


@router.post("/settings/vip/add", response_class=HTMLResponse)
async def vip_add(request: Request, email: str = Form(...)):
    """Suggestion chip -> append the address and re-render the VIP block."""
    async with session_scope() as s:
        await vip_svc.add_entry(s, email)
        ctx = {
            "vip_text": await vip_svc.get_raw(s),
            "vip_placeholder": vip_svc.PLACEHOLDER,
            "vip_suggestions": await vip_svc.suggestions(s),
        }
    return templates.TemplateResponse(request, "settings/_vip.html", ctx)


@router.post("/settings/general/extract-depth", response_class=HTMLResponse)
async def extract_depth_save(days: int = Form(...)):
    days = max(0, min(days, 3650))  # clamp to a sane range (0 = no limit .. 10y)
    async with session_scope() as s:
        await app_prefs.set_extract_backfill_days(s, days)
    msg = "без ограничения" if days == 0 else f"{days} дн."
    return HTMLResponse(f"<span class='export-ok'>✓ Сохранено: {msg}</span>")


@router.post("/settings/general/idle-days", response_class=HTMLResponse)
async def idle_days_save(days: int = Form(...)):
    days = max(0, min(days, 3650))
    async with session_scope() as s:
        await app_prefs.set_idle_days(s, days)
    msg = "не считать" if days == 0 else f"{days} дн."
    return HTMLResponse(f"<span class='export-ok'>✓ Сохранено: {msg}</span>")


@router.post("/settings/general/remind-days", response_class=HTMLResponse)
async def remind_days_save(days: int = Form(...)):
    days = max(0, min(days, 3650))
    async with session_scope() as s:
        await app_prefs.set_remind_days(s, days)
        idle = await app_prefs.get_idle_days(s)
    msg = "не напоминать" if days == 0 else f"{days} дн."
    if days and idle and days >= idle:
        msg += f" — больше порога «затухло» ({idle}), напоминание не успеет раньше"
    return HTMLResponse(f"<span class='export-ok'>✓ Сохранено: {msg}</span>")


@router.post("/settings/general/mail-grouping", response_class=HTMLResponse)
async def mail_grouping_save(grouped: str = Form("")):
    """Снятый чекбокс браузер не присылает вовсе — поэтому пустое значение значит «выкл»,
    а не «не трогать»."""
    on = grouped == "1"
    async with session_scope() as s:
        await settings_store.set_value(s, views.GROUP_MAIL_KEY, "1" if on else "0")
    said = "Группировать по теме" if on else "Списком, без группировки"
    return HTMLResponse(f"<span class='export-ok'>✓ {said}</span>")


@router.post("/settings/general/projects-root", response_class=HTMLResponse)
async def projects_root_save(projects_root: str = Form("")):
    """Where projects without their own folder live. Empty clears it — then such a
    project simply is not exported anywhere, which is a valid state."""
    raw = projects_root.strip()
    if not raw:
        async with session_scope() as s:
            await settings_store.delete_value(s, project_folder.PROJECTS_ROOT_KEY)
        return HTMLResponse("<span class='export-ok'>✓ Очищено</span>")
    try:
        path = project_folder.validate_root(raw)
    except project_folder.ExportBlocked as exc:
        return HTMLResponse(f"<span class='export-err'>{escape(str(exc))}</span>")
    async with session_scope() as s:
        await settings_store.set_value(s, project_folder.PROJECTS_ROOT_KEY, str(path))
    return HTMLResponse(f"<span class='export-ok'>✓ Сохранено: {escape(str(path))}</span>")


@router.post("/settings/meetings", response_class=HTMLResponse)
async def meetings_settings_save(
    space_id: str = Form(""), model_id: str = Form(""), asr_root: str = Form("")
):
    """Which model reads meetings, which space new meetings land in, and where the
    recordings archive is. The shared projects root lives under «Общие»."""
    async with session_scope() as s:
        for key, value in (
            (meetings_svc.SPACE_KEY, space_id.strip()),
            (meetings_svc.MODEL_KEY, model_id.strip()),
            (meetings_svc.ASR_ROOT_KEY, asr_root.strip()),
        ):
            if value:
                await settings_store.set_value(s, key, value)
            else:
                await settings_store.delete_value(s, key)
    return HTMLResponse("<span class='export-ok'>✓ Сохранено</span>")


def _prompt_card_ctx(spec, space_id: uuid.UUID, value: str, is_custom: bool,
                     status: str = "") -> dict:
    return {
        "space_id": space_id, "key": spec.key, "title": spec.title,
        "description": spec.description, "value": value, "is_custom": is_custom,
        "status": status, "suggestion": spec.suggestion,
        # An empty built-in default means the feature is OFF until filled in, so
        # «стандартный» / «сбросить к стандартному» would say the wrong thing.
        "optional": not spec.default.strip(),
        # Поле — только дополнения владельца; встроенный промпт виден рядом, для чтения.
        "builtin": spec.default,
    }


@router.post("/settings/prompts/{space_id}/{key}", response_class=HTMLResponse)
async def prompt_save(
    request: Request, space_id: uuid.UUID, key: str, body: str = Form(...)
):
    try:
        spec = prompts_svc.spec(key)
    except KeyError:
        return HTMLResponse("Неизвестный промпт", status_code=404)
    async with session_scope() as s:
        if body.strip():
            await prompts_svc.set_prompt(s, key, space_id, body)
        else:
            await prompts_svc.reset_prompt(s, key, space_id)
    return templates.TemplateResponse(
        request, "settings/_prompt_card.html",
        _prompt_card_ctx(spec, space_id, body.strip(), bool(body.strip()), "✓ Сохранено"),
    )


@router.post("/settings/prompts/{space_id}/{key}/reset", response_class=HTMLResponse)
async def prompt_reset(request: Request, space_id: uuid.UUID, key: str):
    try:
        spec = prompts_svc.spec(key)
    except KeyError:
        return HTMLResponse("Неизвестный промпт", status_code=404)
    async with session_scope() as s:
        await prompts_svc.reset_prompt(s, key, space_id)
    return templates.TemplateResponse(
        request, "settings/_prompt_card.html",
        _prompt_card_ctx(spec, space_id, "", False,
                         "Сброшено к стандартному" if spec.default.strip() else "Очищено"),
    )


# --- Пространства --------------------------------------------------------------------


def _uuid_or_none(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if raw.strip() else None
    except ValueError:
        return None


@router.post("/settings/spaces", response_class=HTMLResponse)
async def space_create(request: Request, name: str = Form(...)):
    async with session_scope() as s:
        if name.strip():
            await spaces_svc.create_space(s, name)
    return await _spaces_partial(request)


@router.post("/settings/spaces/{space_id}", response_class=HTMLResponse)
async def space_update(
    request: Request, space_id: uuid.UUID, name: str = Form(""),
    model_connection_id: str = Form(""),
):
    async with session_scope() as s:
        await spaces_svc.update_space(
            s, space_id, name=name or None,
            model_connection_id=_uuid_or_none(model_connection_id),
        )
    return await _spaces_partial(request)


@router.post("/settings/spaces/{space_id}/delete", response_class=HTMLResponse)
async def space_delete(request: Request, space_id: uuid.UUID):
    async with session_scope() as s:
        blocker = await spaces_svc.delete_space(s, space_id)
        ctx = await _spaces_ctx(s)
    ctx["space_error"] = blocker
    return templates.TemplateResponse(request, "_spaces.html", ctx)


@router.post("/settings/accounts/{account_id}/move", response_class=HTMLResponse)
async def account_move(request: Request, account_id: uuid.UUID, space_id: str = Form(...)):
    """A mailbox changes space: its mail, threads and their items go with it."""
    target = _uuid_or_none(space_id)
    async with session_scope() as s:
        if target is not None and await spaces_svc.get_space(s, target) is not None:
            await accounts_svc.update_account(s, account_id, space_id=target)
    return await _spaces_partial(request)


@router.post("/settings/spaces/{space_id}/bots", response_class=HTMLResponse)
async def bot_create(
    request: Request, space_id: uuid.UUID, name: str = Form(""), token: str = Form(...),
    chat_id: str = Form(""),
):
    async with session_scope() as s:
        if token.strip() and await spaces_svc.get_space(s, space_id) is not None:
            await spaces_svc.create_bot(
                s, space_id, name=name, token=token, chat_id=_chat_id(chat_id)
            )
    return await _spaces_partial(request)


@router.post("/settings/bots/{bot_id}", response_class=HTMLResponse)
async def bot_update(
    request: Request, bot_id: uuid.UUID, name: str = Form(""), token: str = Form(""),
    chat_id: str = Form(""), enabled: str = Form(""),
):
    async with session_scope() as s:
        await spaces_svc.update_bot(
            s, bot_id, name=name or None, token=token or None, chat_id=_chat_id(chat_id),
            enabled=bool(enabled),
        )
    return await _spaces_partial(request)


@router.post("/settings/bots/{bot_id}/delete", response_class=HTMLResponse)
async def bot_delete(request: Request, bot_id: uuid.UUID):
    async with session_scope() as s:
        await spaces_svc.delete_bot(s, bot_id)
    return await _spaces_partial(request)


async def _models_section(request: Request):
    async with session_scope() as s:
        conns = await mc_svc.list_connections(s)
    return templates.TemplateResponse(request, "_models.html", {"connections": conns})


@router.get("/settings/model/connect/{provider}", response_class=HTMLResponse)
async def model_wizard(request: Request, provider: str):
    preset = ai_providers_mod.AI_PROVIDERS.get(
        provider, ai_providers_mod.AI_PROVIDERS["openai_custom"]
    )
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
    return templates.TemplateResponse(
        request,
        "settings_model_connect.html",
        {**shell, "view": "settings", "provider": provider, "preset": preset,
         "conn": None},
    )


@router.get("/settings/models/{cid}/edit", response_class=HTMLResponse)
async def model_edit_page(request: Request, cid: uuid.UUID):
    async with session_scope() as s:
        conn = await mc_svc.get_connection(s, cid)
        shell = await shell_ctx(s, request)
    if conn is None:
        return RedirectResponse("/settings/section/ai", status_code=303)
    preset = ai_providers_mod.AI_PROVIDERS.get(
        conn.provider, ai_providers_mod.AI_PROVIDERS["openai_custom"]
    )
    return templates.TemplateResponse(
        request,
        "settings_model_connect.html",
        {**shell, "view": "settings", "provider": conn.provider, "preset": preset,
         "conn": conn},
    )


@router.post("/settings/model/test", response_class=HTMLResponse)
async def model_test(
    request: Request,
    provider: str = Form("openai_custom"),
    base_url: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(...),
    connection_id: str = Form(""),
    no_thinking: str = Form(""),
):
    kind = ai_providers_mod.AI_PROVIDERS.get(
        provider, ai_providers_mod.AI_PROVIDERS["openai_custom"]
    )["kind"]
    key = api_key.strip()
    if not key and connection_id:
        async with session_scope() as s:
            conn = await mc_svc.get_connection(s, uuid.UUID(connection_id))
            if conn:
                key = mc_svc._resolve_key(conn)
    err = await ai_extractor.test_connection(
        kind=kind, base_url=base_url or None, api_key=key, model=model,
        no_thinking=bool(no_thinking),
    )
    return templates.TemplateResponse(
        request, "_ai_result.html", {"error": err, "model": model}
    )


@router.post("/settings/models")
async def create_model(
    name: str = Form(...),
    provider: str = Form("openai_custom"),
    base_url: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(...),
    no_thinking: str = Form(""),
):
    async with session_scope() as s:
        await mc_svc.create_connection(
            s, name=name, provider=provider, base_url=base_url, api_key=api_key, model=model,
            no_thinking=bool(no_thinking),
        )
    return RedirectResponse("/settings/section/ai", status_code=303)


@router.post("/settings/models/{cid}")
async def update_model(
    cid: uuid.UUID,
    name: str = Form(...),
    base_url: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(...),
    no_thinking: str = Form(""),
):
    async with session_scope() as s:
        await mc_svc.update_connection(
            s, cid, name=name, base_url=base_url, api_key=api_key or None, model=model,
            no_thinking=bool(no_thinking),
        )
    return RedirectResponse("/settings/section/ai", status_code=303)


@router.post("/settings/models/{cid}/default", response_class=HTMLResponse)
async def model_default(request: Request, cid: uuid.UUID):
    async with session_scope() as s:
        await mc_svc.set_default(s, cid)
    return await _models_section(request)


@router.post("/settings/models/{cid}/delete", response_class=HTMLResponse)
async def model_delete(request: Request, cid: uuid.UUID):
    async with session_scope() as s:
        await mc_svc.delete_connection(s, cid)
    return await _models_section(request)


async def _labels_section(request: Request, applied: int | None = None):
    async with session_scope() as s:
        label_list = await labels_svc.list_labels(s)
        rules = await labels_svc.list_rules(s)
    return templates.TemplateResponse(
        request, "_labels.html", {"labels": label_list, "rules": rules, "applied": applied}
    )


@router.post("/settings/labels", response_class=HTMLResponse)
async def add_label(request: Request, name: str = Form(...), color: str = Form("#9aa0a6")):
    async with session_scope() as s:
        await labels_svc.create_label(s, name, color)
    return await _labels_section(request)


@router.post("/settings/labels/{label_id}/delete", response_class=HTMLResponse)
async def del_label(request: Request, label_id: uuid.UUID):
    async with session_scope() as s:
        await labels_svc.delete_label(s, label_id)
    return await _labels_section(request)


@router.post("/settings/rules", response_class=HTMLResponse)
async def add_rule(
    request: Request,
    match_type: str = Form("address"),
    pattern: str = Form(...),
    label_id: str = Form(...),
):
    async with session_scope() as s:
        await labels_svc.create_rule(s, match_type, pattern, uuid.UUID(label_id))
    return await _labels_section(request)


@router.post("/settings/rules/{rule_id}/delete", response_class=HTMLResponse)
async def del_rule(request: Request, rule_id: uuid.UUID):
    async with session_scope() as s:
        await labels_svc.delete_rule(s, rule_id)
    return await _labels_section(request)


@router.post("/settings/rules/apply", response_class=HTMLResponse)
async def apply_rules(request: Request):
    async with session_scope() as s:
        n = await labels_svc.apply_rules_to_existing(s)
    return await _labels_section(request, applied=n)


@router.post("/settings/identities", response_class=HTMLResponse)
async def add_identity(request: Request, email: str = Form(...)):
    async with session_scope() as s:
        await identities_svc.add_identity(s, email)
        idents = await identities_svc.list_identities(s)
    return templates.TemplateResponse(
        request, "_identities.html", {"identities": idents}
    )


@router.post("/settings/identities/{identity_id}/delete", response_class=HTMLResponse)
async def delete_identity(request: Request, identity_id: uuid.UUID):
    async with session_scope() as s:
        await identities_svc.remove_identity(s, identity_id)
        idents = await identities_svc.list_identities(s)
    return templates.TemplateResponse(
        request, "_identities.html", {"identities": idents}
    )


# --- Storage: manual LOCAL pruning of old mail (server is never touched) ---

def _parse_period(amount: str, unit: str) -> tuple[int, str]:
    try:
        n = max(1, int(amount))
    except (TypeError, ValueError):
        n = 1
    u = unit if unit in ("days", "months") else "months"
    return n, u


@router.post("/settings/storage/preview", response_class=HTMLResponse)
async def storage_preview(
    request: Request, amount: str = Form("3"), unit: str = Form("months")
):
    n, u = _parse_period(amount, unit)
    before = maintenance.cutoff_for(n, u)
    async with session_scope() as s:
        prev = await maintenance.preview_prune(s, before)
    return templates.TemplateResponse(
        request, "_storage_result.html",
        {"preview": prev, "amount": n, "unit": u},
    )


@router.post("/settings/storage/prune", response_class=HTMLResponse)
async def storage_prune(
    request: Request, amount: str = Form("3"), unit: str = Form("months")
):
    n, u = _parse_period(amount, unit)
    before = maintenance.cutoff_for(n, u)
    async with session_scope() as s:
        result = await maintenance.prune_local_before(s, before)
        stats = await maintenance.local_stats(s)
    return templates.TemplateResponse(
        request, "_storage_result.html",
        {"result": result, "stats": stats, "amount": n, "unit": u},
    )


@router.post("/settings/storage/compact", response_class=HTMLResponse)
async def storage_compact(request: Request):
    result = await maintenance.compact()
    async with session_scope() as s:
        stats = await maintenance.local_stats(s)
    return templates.TemplateResponse(
        request, "_storage_result.html", {"compact": result, "stats": stats},
    )


@router.post("/settings/security/mcp", response_class=HTMLResponse)
async def security_mcp(request: Request, action: str = Form("")):
    """Turn the MCP endpoint on or off and hand out its token.

    The token is shown once, right here: only its digest is kept, so a lost one is
    replaced rather than looked up.
    """
    token = None
    async with session_scope() as s:
        if action == "enable":
            await mcp_auth.set_enabled(s, True)
            if not await mcp_auth.has_token(s):
                token = await mcp_auth.issue(s)
        elif action == "disable":
            await mcp_auth.set_enabled(s, False)
        elif action == "new-token":
            token = await mcp_auth.issue(s)
            await mcp_auth.set_enabled(s, True)
        elif action == "revoke":
            await mcp_auth.revoke(s)
            await mcp_auth.set_enabled(s, False)
        ctx = {
            "mcp_enabled": await mcp_auth.is_enabled(s),
            "mcp_has_token": await mcp_auth.has_token(s),
            "mcp_token": token,
            "mcp_url": f"http://{get_settings().web_host}:{get_settings().web_port}/mcp",
        }
    return templates.TemplateResponse(request, "settings/security.html", ctx)


@router.post("/settings/storage/reindex", response_class=HTMLResponse)
async def storage_reindex(request: Request):
    await search_index.rebuild(session_scope)
    async with session_scope() as s:
        written = await search_index.counts(s)
        stats = await maintenance.local_stats(s)
    return templates.TemplateResponse(
        request, "_storage_result.html",
        {"reindex": written, "reindex_total": sum(written.values()), "stats": stats},
    )


@router.get("/settings/connect/{provider}", response_class=HTMLResponse)
async def connect_wizard(request: Request, provider: str):
    preset = providers_mod.PROVIDERS.get(provider, providers_mod.PROVIDERS["imap"])
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
    return templates.TemplateResponse(
        request,
        "settings_connect.html",
        {**shell, "view": "settings", "provider": provider, "preset": preset,
         "account": None, "space_id": shell["space"].id},
    )


@router.post("/settings/test", response_class=HTMLResponse)
async def test_conn(
    request: Request,
    host: str = Form(...),
    port: int = Form(993),
    login: str = Form(...),
    password: str = Form(...),
    account_id: str = Form(""),
):
    # On edit with a blank password, test using the stored one.
    pw = password
    if not pw and account_id:
        async with session_scope() as s:
            acc = await accounts_svc.get_account(s, uuid.UUID(account_id))
            if acc:
                pw = accounts_svc.resolve_password(acc)
    result = await providers_mod.test_connection(host, port, login, pw)
    return templates.TemplateResponse(
        request, "_test_result.html", {"r": result}
    )


@router.post("/settings/test-calendar", response_class=HTMLResponse)
async def test_calendar(
    request: Request,
    provider: str = Form(""),
    login: str = Form(...),
    caldav_password: str = Form(""),
    password: str = Form(""),
    account_id: str = Form(""),
):
    """Test CalDAV auth for the account being edited. Uses the calendar password if
    typed, else the mailbox password (typed or stored) — mirroring resolve_caldav_password."""
    from email_reader.connectors import caldav

    base = caldav.caldav_base_url(provider)
    if not base:
        html = "<div class='tr err'>У этого провайдера нет календаря CalDAV.</div>"
        return HTMLResponse(html)
    pw = caldav_password or password
    if not pw and account_id:
        async with session_scope() as s:
            acc = await accounts_svc.get_account(s, uuid.UUID(account_id))
            if acc:
                pw = accounts_svc.resolve_caldav_password(acc)
    err = await caldav.test_connection(base, login, pw)
    if err is None:
        return HTMLResponse("<div class='tr ok'>✓ Календарь подключается.</div>")
    from markupsafe import escape

    return HTMLResponse(f"<div class='tr err'>Ошибка календаря: {escape(err)}</div>")


def _folders(inbox: str, sent: str) -> list[str]:
    out = []
    for f in (inbox.strip(), sent.strip()):
        if f and f not in out:
            out.append(f)
    return out


def _chat_id(raw: str) -> int | None:
    raw = raw.strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


@router.post("/settings/accounts")
async def create_account(
    name: str = Form(...),
    provider: str = Form("imap"),
    host: str = Form(...),
    port: int = Form(993),
    smtp_host: str = Form(""),
    smtp_port: int = Form(465),
    login: str = Form(...),
    password: str = Form(...),
    caldav_password: str = Form(""),
    inbox: str = Form("INBOX"),
    sent: str = Form(""),
    space_id: str = Form(""),
):
    async with session_scope() as s:
        await accounts_svc.create_account(
            s, name=name, provider=provider, host=host, port=port,
            smtp_host=smtp_host, smtp_port=smtp_port,
            login=login, password=password, folders=_folders(inbox, sent),
            caldav_password=caldav_password or None,
            space_id=_uuid_or_none(space_id),
        )
    return RedirectResponse("/settings/section/spaces", status_code=303)


@router.get("/settings/accounts/{account_id}/edit", response_class=HTMLResponse)
async def edit_account_page(request: Request, account_id: uuid.UUID):
    async with session_scope() as s:
        acc = await accounts_svc.get_account(s, account_id)
    if acc is None:
        return RedirectResponse("/settings/section/spaces", status_code=303)
    preset = providers_mod.PROVIDERS.get(acc.provider, providers_mod.PROVIDERS["imap"])
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
    return templates.TemplateResponse(
        request,
        "settings_connect.html",
        {**shell, "view": "settings", "provider": acc.provider, "preset": preset,
         "account": acc, "space_id": acc.space_id},
    )


@router.post("/settings/accounts/{account_id}")
async def update_account(
    account_id: uuid.UUID,
    name: str = Form(...),
    login: str = Form(...),
    host: str | None = Form(None),
    port: int | None = Form(None),
    smtp_host: str | None = Form(None),
    smtp_port: int | None = Form(None),
    password: str = Form(""),  # blank = keep existing
    caldav_password: str = Form(""),  # blank = keep existing
    inbox: str | None = Form(None),
    sent: str | None = Form(None),
    space_id: str = Form(""),
):
    async with session_scope() as s:
        await accounts_svc.update_account(
            s, account_id, name=name, host=host, port=port,
            smtp_host=smtp_host, smtp_port=smtp_port, login=login,
            password=password or None,
            folders=_folders(inbox, sent or "") if inbox is not None else None,
            caldav_password=caldav_password or None,
            space_id=_uuid_or_none(space_id),
        )
    return RedirectResponse("/settings/section/spaces", status_code=303)


@router.post("/settings/accounts/{account_id}/pause", response_class=HTMLResponse)
async def pause_account(request: Request, account_id: uuid.UUID):
    async with session_scope() as s:
        acc = await accounts_svc.get_account(s, account_id)
        if acc:
            await accounts_svc.update_account(s, account_id, enabled=not acc.enabled)
    return await _spaces_partial(request)


@router.post("/settings/accounts/{account_id}/delete", response_class=HTMLResponse)
async def delete_account(request: Request, account_id: uuid.UUID):
    async with session_scope() as s:
        await accounts_svc.delete_account(s, account_id)
    return await _spaces_partial(request)


# --- Доски (docs/ux-tasks.md §17.5) --------------------------------------------------


async def _boards_ctx(s, request: Request) -> dict:
    """Список досок открытого пространства. Настройка на пространство: доски — его
    устройство, и «Работе» нужны не те же, что «Дому»."""
    space = await current_space(s, request)
    specs = await boards_svc.list_specs(s, space.id)
    return {
        "board_space": space,
        "board_specs": specs,
        "board_custom": sum(1 for b in specs if not b.builtin),
        "board_max": boards_svc.MAX_CUSTOM,
        "board_conditions": boards_svc.CONDITIONS,
        "board_sorts": boards_svc.SORTS,
        "board_icons": boards_svc.ICON_CHOICES,
        "board_types": [(str(t), TYPE_LABEL.get(str(t), str(t))) for t in ACTIONABLE_TYPES],
    }


async def _boards_body(request: Request):
    async with session_scope() as s:
        ctx = await _boards_ctx(s, request)
    return templates.TemplateResponse(request, "settings/_boards.html", ctx)


@router.post("/settings/boards/{key}", response_class=HTMLResponse)
async def board_save(
    request: Request, key: str, name: str = Form(""), icon: str = Form(""),
    hidden: str = Form(""), condition: str = Form(""), sort: str = Form(""),
    types: list[str] = _TYPES_FORM,
):
    """Сохранить доску. У пресета читаются только имя, иконка и видимость — его предикат и
    секции живут в Python (`views.py`), см. §17.5."""
    async with session_scope() as s:
        space = await current_space(s, request)
        await boards_svc.update_spec(
            s, space.id, key, name=name or None, icon=icon or None, hidden=hidden == "1",
            condition=condition, sort=sort, types=types or None,
        )
    return await _boards_body(request)


@router.post("/settings/boards/{key}/delete", response_class=HTMLResponse)
async def board_delete(request: Request, key: str):
    async with session_scope() as s:
        space = await current_space(s, request)
        await boards_svc.delete_spec(s, space.id, key)
    return await _boards_body(request)


@router.post("/settings/boards/{key}/clone", response_class=HTMLResponse)
async def board_clone(request: Request, key: str):
    async with session_scope() as s:
        space = await current_space(s, request)
        await boards_svc.clone_as_filter(s, space.id, key)
    return await _boards_body(request)


@router.post("/settings/boards-new", response_class=HTMLResponse)
async def board_new(
    request: Request, name: str = Form(""), icon: str = Form("dot"),
    condition: str = Form(""), sort: str = Form(boards_svc.DEFAULT_SORT),
    types: list[str] = _TYPES_FORM,
):
    async with session_scope() as s:
        space = await current_space(s, request)
        await boards_svc.create_filter(
            s, space.id, name=name, icon=icon, types=types, condition=condition, sort=sort,
        )
    return await _boards_body(request)


@router.post("/settings/boards-order", response_class=HTMLResponse)
async def boards_order(request: Request, keys: str = Form("")):
    async with session_scope() as s:
        space = await current_space(s, request)
        await boards_svc.reorder(s, space.id, [k for k in keys.split(",") if k])
    return await _boards_body(request)
