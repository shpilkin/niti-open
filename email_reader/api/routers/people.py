"""Люди: the list of everyone a space has seen, the profile pane behind each row, and the
memory the user keeps about them.

The list is derived (memory/directory.py) — profiles are created lazily, when a note is
written or two rows are merged. Everything a row can do lives here: merge with undo,
detach a wrongly merged identity, notes, sender rules.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from email_reader import labels as labels_svc
from email_reader import views
from email_reader.api.common import current_space, shell_ctx, templates
from email_reader.db import session_scope
from email_reader.enums import IdentityKind
from email_reader.memory import directory as dir_svc
from email_reader.memory import facts
from email_reader.memory import people as people_svc
from email_reader.memory import remember as remember_svc
from email_reader.memory import rules as rules_svc
from email_reader.memory import stats as stats_svc
from email_reader.models import AgentDecision, Label, Person

router = APIRouter()


# --- The screen ---------------------------------------------------------------------


async def _list_ctx(s, space_id: uuid.UUID, q: str) -> dict:
    rows = await dir_svc.list_people(s, space_id, q)
    return {"people": rows, "q": q, "people_total": len(rows)}


@router.get("/people", response_class=HTMLResponse)
async def people_page(request: Request, q: str = ""):
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
        ctx = await _list_ctx(s, shell["space"].id, q)
    return templates.TemplateResponse(
        request, "people.html",
        {**shell, "view": "people", "active_key": "people",
         "title_override": "Контакты", **ctx},
    )


@router.get("/people/list", response_class=HTMLResponse)
async def people_list(request: Request, q: str = ""):
    async with session_scope() as s:
        space = await current_space(s, request)
        ctx = await _list_ctx(s, space.id, q)
    return templates.TemplateResponse(request, "_peoplelist.html", ctx)


# --- The pane -----------------------------------------------------------------------


async def _row_for(s, space_id: uuid.UUID, *, key: str = "", q: str = ""):
    """The list row a pane was opened for: by key from «Люди», by counterparty string from
    a card's pane. A counterparty nobody has merged yet gets a row of its own."""
    if key:
        row = await dir_svc.row_for_key(s, space_id, key)
        if row is not None:
            return row
        kind, _, value = key.partition(":")
        if kind == "person":
            person = await s.get(Person, uuid.UUID(value)) if value else None
            if person is not None:
                idents = await people_svc.identities_of(s, person.id)
                return dir_svc.PersonRow(
                    key=key, person_id=person.id,
                    name=people_svc.display_name(person, idents),
                    identities=[(i.kind, i.value, i.display) for i in idents],
                    has_notes=bool(await facts.people_with_notes(s, [person.id])),
                )
            return None
        return dir_svc.PersonRow(key=key, name=value, identities=[(kind, value, None)])

    person = await people_svc.resolve(s, q)
    if person is not None:
        return await _row_for(s, space_id, key=f"person:{person.id}")
    ident = people_svc.identity_of(q)
    if ident is None:
        return None
    kind, value = ident
    return await _row_for(s, space_id, key=dir_svc.identity_key(kind, value))


async def _pane_ctx(s, request: Request, space_id: uuid.UUID, row, back_item: str = "") -> dict:
    addresses, names = dir_svc.identities_for(row) if row else ([], [])
    dossier = await views.person_dossier_ids(
        s, addresses=addresses, names=names, space_id=space_id
    )
    return {
        "row": row, "q": row.name if row else "",
        "items": dossier["items"], "emails": dossier["emails"],
        "meetings": dossier["meetings"], "back_item": back_item,
        "space": str(space_id),
    }


@router.get("/person", response_class=HTMLResponse)
async def person_pane(request: Request, q: str = "", key: str = "", item: str = ""):
    """The person's profile: what the space knows about them plus the memory kept on them.

    Reachable from «Люди» (by key) and from a card's counterparty (by name or address).
    Scoped to the space of the card it was opened from, else the open space."""
    async with session_scope() as s:
        ai = None
        if item:
            try:
                ai = await views.get_item(s, uuid.UUID(item))
            except ValueError:
                ai = None
        space_id = ai.space_id if ai else (await current_space(s, request)).id
        row = await _row_for(s, space_id, key=key, q=q)
        ctx = await _pane_ctx(s, request, space_id, row, item)
        if row is None:
            ctx["q"] = q
    return templates.TemplateResponse(request, "_personpane.html", ctx)


# --- Memory (the block inside the pane) ----------------------------------------------


async def _memory_ctx(s, row) -> dict:
    """The «Память» block: notes on the profile (any identity), plus the address-keyed
    facts and rules where there is an address to key them on."""
    person = await s.get(Person, row.person_id) if row and row.person_id else None
    addrs = row.emails if row else []
    addr = (person.email if person and person.email in addrs else None) or next(
        iter(addrs), None
    )
    notes = await people_svc.notes_of(s, person) if person is not None else ""
    return {"row": row, "person": person, "email": addr, "notes": notes}


async def _render_memory(request: Request, s, row, space_id: uuid.UUID):
    ctx = await _memory_ctx(s, row)
    addr = ctx["email"]
    stats = await stats_svc.person_stats(s, addr, space_id) if addr else None
    rules = []
    for r in (await labels_svc.rules_for_address(s, addr) if addr else []):
        value = r.value
        if r.action == "label" and r.label_id is not None:
            lbl = await s.get(Label, r.label_id)
            value = lbl.name if lbl else None
        rules.append((r, rules_svc.describe(r.action, value)))
    proposals = await rules_svc.propose_for(s, space_id, addr) if addr else []
    return templates.TemplateResponse(
        request, "_personmemory.html",
        {**ctx, "name": row.name if row else "", "stats": stats,
         "facts": stats_svc.describe(stats) if stats else [],
         "space": str(space_id), "rules": rules, "proposals": proposals},
    )


async def _space_from_form(s, request: Request, space: str) -> uuid.UUID:
    try:
        return uuid.UUID(space) if space else (await current_space(s, request)).id
    except ValueError:
        return (await current_space(s, request)).id


@router.get("/person/memory", response_class=HTMLResponse)
async def person_memory(request: Request, q: str = "", key: str = "", item: str = ""):
    async with session_scope() as s:
        ai = None
        if item:
            try:
                ai = await views.get_item(s, uuid.UUID(item))
            except ValueError:
                ai = None
        space_id = ai.space_id if ai else (await current_space(s, request)).id
        row = await _row_for(s, space_id, key=key, q=q)
        return await _render_memory(request, s, row, space_id)


@router.post("/person/notes", response_class=HTMLResponse)
async def person_notes(
    request: Request, key: str = Form(""), notes: str = Form(""), space: str = Form(""),
):
    """Save what the user knows. This is what creates the profile for somebody who until
    now was just a name on a card — no address required (docs/feature-people.md §4)."""
    async with session_scope() as s:
        space_id = await _space_from_form(s, request, space)
        row = await _row_for(s, space_id, key=key)
        if row is None:
            return HTMLResponse("<div class='pane-empty'>Контакт не найден.</div>", 404)
        person = await _ensure_profile(s, row)
        if person is None:
            return HTMLResponse("<div class='pane-empty'>Не к кому привязать.</div>", 400)
        await people_svc.set_notes(s, person, notes)
        row = await _row_for(s, space_id, key=f"person:{person.id}")
        return await _render_memory(request, s, row, space_id)


async def _ensure_profile(s, row) -> Person | None:
    """The profile behind a list row, created on demand — a row is only a derived line
    until the user does something that has to be remembered."""
    if row.person_id:
        return await s.get(Person, row.person_id)
    kind, value, display = row.identities[0]
    if kind == IdentityKind.EMAIL.value:
        return await people_svc.ensure_person(s, value, display or row.name)
    return await people_svc.ensure_person_by_name(s, display or row.name)


# --- Merge --------------------------------------------------------------------------


def _survivor(rows: list) -> int:
    """Which profile absorbs the others: the one with notes, then the one with an address,
    then the most active. Deterministic, so «Объединить» never asks a question the user
    cannot answer — the result is on screen at once and «Отменить» is one click."""
    def rank(r):
        return (
            r.has_notes, bool(r.emails), r.total_items,
            r.last_at.timestamp() if r.last_at else 0,
        )
    return max(range(len(rows)), key=lambda i: rank(rows[i]))


@router.post("/people/merge", response_class=HTMLResponse)
async def people_merge(request: Request, keys: str = Form(""), q: str = Form("")):
    """Fold the selected rows into one human being."""
    async with session_scope() as s:
        space = await current_space(s, request)
        rows = []
        for key in [k for k in keys.split(",") if k.strip()]:
            row = await _row_for(s, space.id, key=key)
            if row is not None:
                rows.append(row)
        if len(rows) < 2:
            ctx = await _list_ctx(s, space.id, q)
            return templates.TemplateResponse(request, "_peoplelist.html", ctx)
        pairs = [(r, await _ensure_profile(s, r)) for r in rows]
        pairs = [(r, p) for r, p in pairs if p is not None]
        if len(pairs) < 2:
            ctx = await _list_ctx(s, space.id, q)
            return templates.TemplateResponse(request, "_peoplelist.html", ctx)
        keep_i = _survivor([r for r, _ in pairs])
        keep = pairs[keep_i][1]
        drops = [p.id for i, (_, p) in enumerate(pairs) if i != keep_i]
        merged, undo = await people_svc.merge(s, keep.id, drops)
        s.add(AgentDecision(kind="user_action", payload={
            "action": "people_merge", "keep": str(keep.id),
            "dropped": [str(d) for d in drops],
        }))
        name = people_svc.display_name(merged) if merged else ""
        ctx = await _list_ctx(s, space.id, q)
        ctx["undo"] = {
            "url": "/people/unmerge", "label": f"Объединено в «{name}»",
            "values": {"undo": json.dumps(undo), "q": q},
        }
    return _list_response(request, ctx)


@router.post("/people/unmerge", response_class=HTMLResponse)
async def people_unmerge(request: Request, undo: str = Form("{}"), q: str = Form("")):
    async with session_scope() as s:
        space = await current_space(s, request)
        try:
            payload = json.loads(undo or "{}")
        except ValueError:
            payload = {}
        await people_svc.unmerge(s, payload)
        ctx = await _list_ctx(s, space.id, q)
        ctx["toast"] = "Объединение отменено"
    return _list_response(request, ctx)


@router.post("/person/detach", response_class=HTMLResponse)
async def person_detach(
    request: Request, key: str = Form(""), kind: str = Form(""), value: str = Form(""),
    space: str = Form(""),
):
    """Pull one identity out of a profile — the fix for a merge regretted later."""
    async with session_scope() as s:
        space_id = await _space_from_form(s, request, space)
        row = await _row_for(s, space_id, key=key)
        if row is None or not row.person_id:
            return HTMLResponse("<div class='pane-empty'>Профиль не найден.</div>", 404)
        await people_svc.detach(s, row.person_id, kind, value)
        row = await _row_for(s, space_id, key=f"person:{row.person_id}")
        ctx = await _pane_ctx(s, request, space_id, row)
        resp = templates.TemplateResponse(request, "_personpane.html", ctx)
        resp.headers["HX-Trigger"] = json.dumps({"countsChanged": True})
        return resp


def _list_response(request: Request, ctx: dict):
    trigger: dict = {"countsChanged": True}
    if ctx.get("undo"):
        trigger["showUndo"] = ctx["undo"]
    if ctx.get("toast"):
        trigger["showToast"] = ctx["toast"]
    resp = templates.TemplateResponse(request, "_peoplelist.html", ctx)
    resp.headers["HX-Trigger"] = json.dumps(trigger)
    return resp


# --- Sender rules (unchanged behaviour, address-keyed) --------------------------------


@router.post("/person/rules", response_class=HTMLResponse)
async def person_rule_add(
    request: Request, key: str = Form(""), space: str = Form(""),
    match_type: str = Form("address"), pattern: str = Form(""), action: str = Form("ignore"),
    value: str = Form(""),
):
    """A confirmed proposal or a hand-typed rule for this sender. The pattern is the
    address; a domain rule takes its domain."""
    async with session_scope() as s:
        space_id = await _space_from_form(s, request, space)
        row = await _row_for(s, space_id, key=key)
        addr = next(iter(row.emails), None) if row else None
        if not addr:
            return HTMLResponse("<div class='pane-empty'>Адреса нет.</div>", status_code=400)
        pat = people_svc.canonical(pattern) or addr
        if match_type == "domain" and "@" in pat:
            pat = pat.split("@", 1)[1]
        rule = await rules_svc.accept(
            s, match_type=match_type, pattern=pat, action=action, value=value or None
        )
        if rule is not None:
            s.add(AgentDecision(kind="user_action", payload={
                "action": "rule_add", "rule_id": str(rule.id), "match_type": rule.match_type,
                "pattern": rule.pattern, "rule": rule.action, "value": rule.value,
            }))
        return await _render_memory(request, s, row, space_id)


@router.post("/person/rules/{rule_id}/delete", response_class=HTMLResponse)
async def person_rule_delete(
    request: Request, rule_id: uuid.UUID, key: str = Form(""), space: str = Form(""),
):
    async with session_scope() as s:
        space_id = await _space_from_form(s, request, space)
        row = await _row_for(s, space_id, key=key)
        await labels_svc.delete_rule(s, rule_id)
        s.add(AgentDecision(
            kind="user_action", payload={"action": "rule_delete", "rule_id": str(rule_id)},
        ))
        return await _render_memory(request, s, row, space_id)


# --- «Запомнить» from a card ----------------------------------------------------------


@router.post("/item/{item_id}/remember", response_class=HTMLResponse)
async def item_remember(
    request: Request, item_id: uuid.UUID, target: str = Form("profile"), text: str = Form(""),
):
    """One line into memory from a card: about the counterparty (the person's profile) or
    into the space profile. Re-renders the block with the confirmation."""
    async with session_scope() as s:
        ai = await views.get_item(s, item_id)
        if not ai:
            return HTMLResponse(
                "<div class='pane-empty'>Карточка не найдена.</div>", status_code=404
            )
        addr, name = await remember_svc.counterparty_target(s, ai)
        note = " ".join((text or "").split())
        remembered = (
            await remember_svc.remember(s, item=ai, target=target, text=note, addr=addr, name=name)
            if note else None
        )
        return templates.TemplateResponse(
            request, "_remember.html",
            {"item": ai, "remember_addr": addr, "remember_name": name, "remembered": remembered},
        )
