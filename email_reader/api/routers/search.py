"""Search: the results page in the list column, and the ⌘K palette over everything.

Both call the same `search.query.search` — the palette only asks for fewer rows per kind
and lets the last word match as a prefix, because there the user is still typing.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from email_reader.api.common import current_space, templates
from email_reader.db import session_scope
from email_reader.search import query as search_query
from email_reader.search.index import KINDS

router = APIRouter()

# Chips over the results: a subset of KINDS, in the order they are offered.
CHIPS: tuple[tuple[str, str], ...] = (
    ("mail", "Письма"),
    ("card", "Обязательства"),
    ("meeting", "Встречи"),
    ("turn", "Моменты"),
    ("event", "Календарь"),
    ("person", "Контакты"),
    ("project", "Проекты"),
    ("fact", "Память"),
)


# Сколько строк просим у поиска. Ровно столько в ответе значит «найдено не меньше»:
# сказать «найдено 200», когда их триста, — соврать.
PAGE = 200


def _context(line: str, parsed, results, *, by_date: bool) -> dict:  # noqa: ANN001
    return {
        "q": line,
        "parsed": parsed,
        "groups": search_query.grouped(results),
        "total": len(results),
        "capped": len(results) >= PAGE,
        "chips": CHIPS,
        "active_kinds": parsed.kinds,
        "by_date": by_date,
        "KIND_LABEL": {k: v[0] for k, v in KINDS.items()},
        "view": "search",
    }


@router.get("/search", response_class=HTMLResponse)
async def search_results(request: Request, q: str = "", sort: str = ""):
    line = (q or "").strip()
    parsed, results = search_query.parse(line), []
    if line:
        async with session_scope() as s:
            space = await current_space(s, request)
            parsed, results = await search_query.search(
                s, line, space_id=space.id, limit=PAGE, by_date=sort == "date",
            )
    return templates.TemplateResponse(
        request, "_searchresults.html", _context(line, parsed, results, by_date=sort == "date"),
    )


@router.get("/search/palette", response_class=HTMLResponse)
async def search_palette(request: Request, q: str = ""):
    """The ⌘K drop-down: a few rows per kind, the last word treated as a prefix."""
    line = (q or "").strip()
    parsed, results = search_query.parse(line), []
    if line:
        async with session_scope() as s:
            space = await current_space(s, request)
            parsed, results = await search_query.search(
                s, line, space_id=space.id, limit=24, per_kind=4, prefix=True,
            )
    return templates.TemplateResponse(
        request, "_palette_results.html",
        {"q": line, "groups": search_query.grouped(results), "total": len(results),
         "KIND_LABEL": {k: v[0] for k, v in KINDS.items()}},
    )
