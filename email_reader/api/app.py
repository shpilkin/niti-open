"""FastAPI app: ChatGPT-style UI over the commitment views + mailbox connection wizard."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from email_reader import boards as boards_svc
from email_reader import meetings as meetings_svc
from email_reader.api.common import (
    BASE_DIR,
    CLOSE_HINT,
    TYPE_LABEL,
    TYPE_META,
    VIEW_TITLES,
    _age,
    _due_state,
    _hl,
    _list_reason,
    _local,
    _since,
    _size,
    _who_name,
    templates,
)
from email_reader.api.routers import (
    boards,
    briefing,
    calendar,
    compose,
    mail,
    meetings,
    people,
    projects,
    search,
    settings,
)
from email_reader.enums import ACTIONABLE_TYPES
from email_reader.icons import icon as _icon
from email_reader.mcp import server as mcp_server


class _Revalidated(StaticFiles):
    """Статика, которую окно приложения каждый раз сверяет с сервером.

    Без заголовка кэша WebKit считает файл свежим «на глаз» и не спрашивает сервер. Пока
    порт был случайным, это не мешало: новый порт — новый адрес, кэш пуст. С постоянным
    портом окно держало app.css от 19.09 и не видело ни одной правки интерфейса после
    него. `no-cache` — не «не хранить», а «спроси, прежде чем брать»: на localhost ответ
    304 стоит миллисекунду.
    """

    async def get_response(self, path, scope):  # noqa: ANN001, ANN201
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def asset_version(static_dir: Path) -> str:
    """Отпечаток статики для адресов в шаблонах: новый CSS — новый адрес, и старая копия
    в кэше окна не подменит его, даже если кэш не станет её перепроверять."""
    digest = hashlib.sha1()
    for path in sorted(static_dir.rglob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def create_app() -> FastAPI:
    app = FastAPI(title="Niti")
    static_dir = BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", _Revalidated(directory=str(static_dir)), name="static")

    templates.env.globals.update(
        VIEW_TITLES=VIEW_TITLES, TYPE_META=TYPE_META, TYPE_LABEL=TYPE_LABEL, age=_age,
        wait_age=_since, due_state=_due_state, icon=_icon,
        ACTIONABLE_TYPES=ACTIONABLE_TYPES, CLOSE_HINT=CLOSE_HINT,
        board_drop=boards_svc.drop_type, ASSET_V=asset_version(static_dir),
        summary_blocks=meetings_svc.summary_blocks, summary_gist=meetings_svc.summary_gist,
    )
    templates.env.filters["local"] = _local
    templates.env.filters["list_reason"] = _list_reason
    templates.env.filters["who_name"] = _who_name
    templates.env.filters["size"] = _size
    templates.env.filters["hl"] = _hl

    # Register the routers' routes eagerly. FastAPI >=0.128 include_router() keeps a
    # reference (app.routes then holds _IncludedRouter placeholders); the monolith
    # defined every route directly on the app, so keep app.routes flat — the same
    # APIRoute objects, in the same matching order, exactly as @app.<method> produced.
    for router in (boards.router, mail.router, compose.router,
                   projects.router, meetings.router, settings.router, people.router,
                   briefing.router, search.router, calendar.router):
        for route in router.routes:
            route.dependency_overrides_provider = app  # what @app.<method> would set
            app.router.routes.append(route)

    @app.get("/healthz")
    async def healthz():
        return Response("ok")

    # MCP for outside agents. Mounted last so it cannot shadow a UI route, and behind a
    # bearer token issued in Settings -> Безопасность (email_reader.mcp.auth).
    mcp_server.mount(app)

    return app


app = create_app()
