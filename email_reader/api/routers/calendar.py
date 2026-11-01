"""Календарь: день, неделя и месяц — и панель события (docs/feature-calendar.md §5).

Вид, дата и выключенные календари живут в адресе (`view`, `date`, `hide`): страницу
можно обновить, и она откроется на том же месте, а кнопки «назад»/«вперёд» окна листают
так же, как стрелки в шапке.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from email_reader import accounts as accounts_svc
from email_reader import calendar_grid, calendar_view, calendar_write, clock
from email_reader.api.common import current_space, shell_ctx, templates
from email_reader.db import session_scope

router = APIRouter()

VIEWS = ("day", "week", "month")
_KEY = re.compile(r"[0-9a-f]{8}")
VIEW_LABELS = {"day": "День", "week": "Неделя", "month": "Месяц"}
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря")
MONTHS_NOM = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
              "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
WEEKDAYS_FULL = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота",
                 "Воскресенье")


def _anchor(value: str) -> date:
    try:
        return date.fromisoformat(value) if value else clock.today()
    except ValueError:
        return clock.today()


def _hidden(value: str) -> frozenset[str]:
    """Выключенные календари — ключи `calendar_view.source_key`; чужое молча отбрасываем."""
    return frozenset(p for p in (part.strip() for part in (value or "").split(","))
                     if _KEY.fullmatch(p))


def _add_month(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _range(view: str, anchor: date) -> tuple[date, date, date]:
    """Начало показанного отрезка, предыдущий и следующий якорь."""
    if view == "day":
        return anchor, anchor - timedelta(days=1), anchor + timedelta(days=1)
    if view == "month":
        first = anchor.replace(day=1)
        return first, _add_month(first, -1), _add_month(first, 1)
    start = calendar_grid.monday_of(anchor)
    return start, anchor - timedelta(days=7), anchor + timedelta(days=7)


def title(view: str, anchor: date) -> str:
    """Заголовок как в Календаре: месяц крупно, у дня — ещё и число с днём недели."""
    if view == "day":
        return (f"{WEEKDAYS_FULL[anchor.weekday()]}, {anchor.day} "
                f"{MONTHS[anchor.month - 1]} {anchor.year}")
    if view == "month":
        return f"{MONTHS_NOM[anchor.month - 1]} {anchor.year}"
    start = calendar_grid.monday_of(anchor)
    end = start + timedelta(days=6)
    if start.month == end.month:
        return f"{MONTHS_NOM[start.month - 1]} {start.year}"
    if start.year == end.year:
        return f"{MONTHS_NOM[start.month - 1]} – {MONTHS_NOM[end.month - 1]} {end.year}"
    return (f"{MONTHS_NOM[start.month - 1]} {start.year} – "
            f"{MONTHS_NOM[end.month - 1]} {end.year}")


def _mini(anchor: date, month_first: date, shown: tuple[date, date]) -> dict:
    """Маленький календарик для перехода на дату: сетка месяца и что сейчас показано."""
    start, end = calendar_grid.month_bounds(month_first)
    days = [start + timedelta(days=i) for i in range((end - start).days)]
    return {
        "mini_first": month_first,
        "mini_title": f"{MONTHS_NOM[month_first.month - 1]} {month_first.year}",
        "mini_weeks": [days[i:i + 7] for i in range(0, len(days), 7)],
        "mini_prev": _add_month(month_first, -1),
        "mini_next": _add_month(month_first, 1),
        "mini_shown": shown,
    }


def _shown(view: str, anchor: date) -> tuple[date, date]:
    """Первый и последний показанный день — их календарик выделяет."""
    start = _range(view, anchor)[0]
    if view == "month":
        return start, calendar_grid.month_bounds(start)[1] - timedelta(days=1)
    return start, start + timedelta(days=0 if view == "day" else 6)


def _nav(view: str, anchor: date, hidden: frozenset) -> dict:
    """То, что нужно любой ссылке календаря: вид, дата и адрес с изменением."""
    view = view if view in VIEWS else "week"

    def link(**change) -> str:
        """Адрес с тем же видом, датой и выключенными календарями — кроме меняемого."""
        state = {"view": view, "date": anchor.isoformat(), "hide": ",".join(sorted(hidden))}
        state.update({k: (v.isoformat() if isinstance(v, date) else v)
                      for k, v in change.items()})
        return urlencode({k: v for k, v in state.items() if v})

    # `view` занят оболочкой страницы (подсветка пункта меню), поэтому вид календаря —
    # `cal_view`.
    def toggle(key: str) -> str:
        """Значение `hide`, если щёлкнуть по календарю: был виден — спрятать, и наоборот."""
        return ",".join(sorted(hidden ^ {key}))

    return {"cal_view": view, "anchor": anchor, "today": clock.today(), "link": link,
            "toggle": toggle, "hidden": hidden, "WEEKDAYS": WEEKDAYS,
            "all_day": lambda ev: calendar_grid.is_all_day(ev.start_at, ev.end_at)}


async def _ctx(s, space, view: str, anchor: date, hidden: frozenset) -> dict:  # noqa: ANN001
    ctx = _nav(view, anchor, hidden)
    view = ctx["cal_view"]
    now = datetime.now(UTC)
    start, prev, nxt = _range(view, anchor)
    calendars = await calendar_view.calendars(s, space.id)
    colors = {c.key: c.color for c in calendars}
    ctx.update({
        "now": now, "cal_title": title(view, anchor), "space_name": space.name,
        "prev": prev, "next": nxt, "calendars": calendars,
        "color_of": lambda ev: colors.get(calendar_view.key_of(ev), 1),
        "VIEWS": VIEWS, "VIEW_LABELS": VIEW_LABELS,
        "space_accounts": await accounts_svc.list_accounts(s, space.id),
    })
    if view == "month":
        ctx["month"] = await calendar_view.month(s, space.id, anchor, now=now, hidden=hidden)
        ctx["empty"] = not any(d.items for w in ctx["month"].weeks for d in w)
    else:
        ctx["week"] = await calendar_view.week(s, space.id, start,
                                               days=1 if view == "day" else 7,
                                               now=now, hidden=hidden)
        ctx["empty"] = not any(d.events or d.all_day for d in ctx["week"].days)
    ctx.update(_mini(anchor, anchor.replace(day=1), _shown(view, anchor)))
    return ctx


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request, view: str = "week", date: str = "",
                        hide: str = ""):
    async with session_scope() as s:
        shell = await shell_ctx(s, request)
        ctx = {**shell, **await _ctx(s, shell["space"], view, _anchor(date), _hidden(hide))}
    ctx.update(view="calendar", active="calendar", active_key="calendar",
               title_override="Календарь")
    return templates.TemplateResponse(request, "calendar.html", ctx)


@router.get("/calendar/grid", response_class=HTMLResponse)
async def calendar_grid_part(request: Request, view: str = "week", date: str = "",
                             hide: str = ""):
    """Только календарь — для листания и переключения вида без перерисовки страницы."""
    async with session_scope() as s:
        space = await current_space(s, request)
        ctx = await _ctx(s, space, view, _anchor(date), _hidden(hide))
    return templates.TemplateResponse(request, "_calendar.html", ctx)


@router.get("/calendar/mini", response_class=HTMLResponse)
async def calendar_mini(request: Request, month: str, view: str = "week", date: str = "",
                        hide: str = ""):
    """Календарик выбора даты листает свои месяцы, не трогая сам календарь."""
    ctx = _nav(view, _anchor(date), _hidden(hide))
    ctx.update(_mini(ctx["anchor"], _anchor(month).replace(day=1),
                     _shown(ctx["cal_view"], ctx["anchor"])))
    return templates.TemplateResponse(request, "_calmini.html", ctx)


@router.get("/event/{event_id}", response_class=HTMLResponse)
async def event_pane(request: Request, event_id: uuid.UUID):
    async with session_scope() as s:
        space = await current_space(s, request)
        event = await calendar_view.get_event(s, event_id, space.id)
        ctx: dict = {"event": event}
        if event is not None:
            ctx.update(
                mailbox=await calendar_view.mailbox_of(s, event),
                recordings=await calendar_view.recordings_for(s, event, space.id),
                all_day=calendar_grid.is_all_day(event.start_at, event.end_at),
                MONTHS=MONTHS, WEEKDAYS=WEEKDAYS,
                editable=calendar_write.editable(
                    event, await calendar_view.mailbox_of(s, event)),
            )
    return templates.TemplateResponse(request, "_eventpane.html", ctx)


# --- Создание и правка события (docs/feature-calendar.md §11) ---


def _local_tz():
    return datetime.now().astimezone().tzinfo


async def _form_ctx(s, space, event=None, *, day: date | None = None,  # noqa: ANN001
                    start: str = "", end: str = "", all_day: bool = False,
                    error: str = "", values: dict | None = None) -> dict:
    targets = await calendar_write.targets(s, space.id)
    ctx: dict = {"targets": targets, "event": event, "error": error}
    if event is not None:
        local_start = event.start_at.astimezone()
        local_end = (event.end_at or event.start_at + timedelta(hours=1)).astimezone()
        is_all_day = calendar_grid.is_all_day(event.start_at, event.end_at)
        ctx["f"] = {
            "title": event.title or "", "day": (event.start_at.date() if is_all_day
                                                  else local_start.date()).isoformat(),
            "start": local_start.strftime("%H:%M"), "end": local_end.strftime("%H:%M"),
            "all_day": is_all_day, "location": event.location or "",
            "description": calendar_write._clean_description(event.description or ""),
            "participants": ", ".join(calendar_write.participants_of(event)),
            "target": "", "invited": bool(event.attendees),
        }
    else:
        ctx["f"] = {
            "title": "", "day": (day or clock.today()).isoformat(),
            "start": start or "10:00", "end": end or "", "all_day": all_day,
            "location": "", "description": "", "participants": "",
            "target": targets[0].key if targets else "", "invited": False,
        }
        if not ctx["f"]["end"]:
            h, m = (int(x) for x in ctx["f"]["start"].split(":"))
            ctx["f"]["end"] = f"{min(h + 1, 23):02d}:{m:02d}"
    if values:
        ctx["f"].update(values)
    return ctx


@router.get("/calendar/event/new", response_class=HTMLResponse)
async def event_new(request: Request, date: str = "", start: str = "", end: str = "",
                    all_day: int = 0):
    """Окно нового события: из кнопки «+» или щелчком по пустому месту сетки."""
    async with session_scope() as s:
        space = await current_space(s, request)
        ctx = await _form_ctx(s, space, day=_anchor(date), start=start, end=end,
                              all_day=bool(all_day))
    return templates.TemplateResponse(request, "_eventform.html", ctx)


@router.get("/event/{event_id}/edit", response_class=HTMLResponse)
async def event_edit(request: Request, event_id: uuid.UUID):
    async with session_scope() as s:
        space = await current_space(s, request)
        event = await calendar_view.get_event(s, event_id, space.id)
        if event is None:
            return HTMLResponse("<div class='pane-inner'><p class='hint-inline'>"
                                "Событие не найдено в этом пространстве.</p></div>", 404)
        why = calendar_write.editable(event, await calendar_view.mailbox_of(s, event))
        ctx = await _form_ctx(s, space, event, error=why or "")
        ctx["locked"] = bool(why)
    return templates.TemplateResponse(request, "_eventform.html", ctx)


def _form_values(title, day, start, end, all_day, location, description,  # noqa: ANN001
                 participants, invite, target) -> dict:
    return {"title": title, "day": day, "start": start, "end": end, "all_day": bool(all_day),
            "location": location, "description": description,
            "participants": participants, "invite": bool(invite), "target": target}


async def _saved(request: Request, s, space, event) -> HTMLResponse:  # noqa: ANN001
    """После сохранения — панель события и сигнал сетке перерисоваться."""
    ctx = {
        "event": event, "mailbox": await calendar_view.mailbox_of(s, event),
        "recordings": await calendar_view.recordings_for(s, event, space.id),
        "all_day": calendar_grid.is_all_day(event.start_at, event.end_at),
        "MONTHS": MONTHS, "WEEKDAYS": WEEKDAYS,
        "editable": calendar_write.editable(
            event, await calendar_view.mailbox_of(s, event)),
    }
    resp = templates.TemplateResponse(request, "_eventpane.html", ctx)
    resp.headers["HX-Trigger"] = "calendarChanged"
    return resp


@router.post("/calendar/event", response_class=HTMLResponse)
async def event_create(
    request: Request, title: str = Form(""), day: str = Form(""), start: str = Form("10:00"),
    end: str = Form("11:00"), all_day: str = Form(""), location: str = Form(""),
    description: str = Form(""), participants: str = Form(""), invite: str = Form(""),
    target: str = Form(""),
):
    values = _form_values(title, day, start, end, all_day, location, description,
                          participants, invite, target)
    async with session_scope() as s:
        space = await current_space(s, request)
        options = await calendar_write.targets(s, space.id)
        chosen = calendar_write.target_of(options, target)
        try:
            if chosen is None:
                raise calendar_write.WriteError("Выберите календарь.")
            draft = calendar_write.parse_draft(
                title=title, day=day, start=start, end=end, all_day=bool(all_day),
                location=location, description=description, participants=participants,
                invite=invite == "1", tz=_local_tz())
            event = await calendar_write.create(s, chosen, draft)
        except (calendar_write.WriteError, ValueError) as exc:
            ctx = await _form_ctx(s, space, error=str(exc), values=values)
            return templates.TemplateResponse(request, "_eventform.html", ctx)
        await s.commit()
        return await _saved(request, s, space, event)


@router.post("/event/{event_id}", response_class=HTMLResponse)
async def event_update(
    request: Request, event_id: uuid.UUID, title: str = Form(""), day: str = Form(""),
    start: str = Form("10:00"), end: str = Form("11:00"), all_day: str = Form(""),
    location: str = Form(""), description: str = Form(""), participants: str = Form(""),
    invite: str = Form(""),
):
    values = _form_values(title, day, start, end, all_day, location, description,
                          participants, invite, "")
    async with session_scope() as s:
        space = await current_space(s, request)
        event = await calendar_view.get_event(s, event_id, space.id)
        if event is None:
            return HTMLResponse("<div class='pane-inner'><p class='hint-inline'>"
                                "Событие не найдено в этом пространстве.</p></div>", 404)
        try:
            draft = calendar_write.parse_draft(
                title=title, day=day, start=start, end=end, all_day=bool(all_day),
                location=location, description=description, participants=participants,
                invite=invite == "1", tz=_local_tz())
            await calendar_write.update(s, event, draft)
        except (calendar_write.WriteError, ValueError) as exc:
            ctx = await _form_ctx(s, space, event, error=str(exc), values=values)
            return templates.TemplateResponse(request, "_eventform.html", ctx)
        await s.commit()
        return await _saved(request, s, space, event)


@router.get("/event/{event_id}/delete", response_class=HTMLResponse)
async def event_delete_ask(request: Request, event_id: uuid.UUID):
    """Подтверждение удаления: с участниками — выбор, оповещать ли их."""
    async with session_scope() as s:
        space = await current_space(s, request)
        event = await calendar_view.get_event(s, event_id, space.id)
        ctx = {"event": event, "invited": bool(event and event.attendees), "error": ""}
    return templates.TemplateResponse(request, "_eventdelete.html", ctx)


@router.post("/event/{event_id}/delete", response_class=HTMLResponse)
async def event_delete(request: Request, event_id: uuid.UUID, notify: str = Form("")):
    async with session_scope() as s:
        space = await current_space(s, request)
        event = await calendar_view.get_event(s, event_id, space.id)
        if event is None:
            return HTMLResponse("<div class='pane-inner'><p class='hint-inline'>"
                                "Событие не найдено в этом пространстве.</p></div>", 404)
        try:
            await calendar_write.delete(s, event, notify == "1")
        except calendar_write.WriteError as exc:
            ctx = {"event": event, "invited": bool(event.attendees), "error": str(exc)}
            return templates.TemplateResponse(request, "_eventdelete.html", ctx)
        await s.commit()
        return await _saved(request, s, space, event)
