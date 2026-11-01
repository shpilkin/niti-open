"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import greenlet
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from email_reader.config import get_settings

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

# A write transaction held longer than this is logged with the code that started it.
SLOW_WRITE_SECONDS = 5.0
_WRITE_VERBS = ("insert", "update", "delete", "replace", "vacuum")
_WRITE_KEY = "write_started"


def _configure_sqlite(engine: AsyncEngine) -> None:
    """Enforce foreign keys, enable WAL and a busy-timeout on every connection.

    WAL lets the single writer (worker/sync) and readers (the web UI) coexist
    without 'database is locked'; busy_timeout adds a grace window on contention.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        # Generous grace window: the initial bulk sync can hold the single writer for a
        # while, and a short timeout turned transient contention into hard crashes.
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


def _caller() -> str:
    """Where in the app a statement came from, innermost first. SQLAlchemy runs the
    statement in a child greenlet, so the awaiting code is on the parent's stack."""
    parent = greenlet.getcurrent().parent
    frame = parent.gr_frame if parent is not None else sys._getframe(2)
    ours = []
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module.split(".", 1)[0] == "email_reader":
            ours.append(f"{module}:{frame.f_lineno} {frame.f_code.co_name}")
        frame = frame.f_back
    return " ← ".join(ours[:5]) or "?"


def _watch_writes(engine: AsyncEngine) -> None:
    """Log every write transaction that holds SQLite's write lock past SLOW_WRITE_SECONDS:
    while it is held, every other writer waits, and past busy_timeout it fails with
    'database is locked' (docs/feature-storage-plan.md)."""

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _first_write(conn, _cursor, statement, _params, _context, _many):  # noqa: ANN001
        if _WRITE_KEY not in conn.info and statement.lstrip()[:7].lower().startswith(_WRITE_VERBS):
            # The statement too, not just the caller: autoflush sends the write from
            # wherever the next query happens to be, and that place is not the culprit.
            conn.info[_WRITE_KEY] = (time.monotonic(), " ".join(statement.split())[:60], _caller())

    def _finish(info: dict) -> None:
        started = info.pop(_WRITE_KEY, None)
        if started is not None:
            held = time.monotonic() - started[0]
            if held > SLOW_WRITE_SECONDS:
                log.warning(
                    "Write lock held %.1fs by «%s», flushed at %s", held, started[1], started[2]
                )

    event.listen(engine.sync_engine, "commit", lambda conn: _finish(conn.info))
    event.listen(engine.sync_engine, "rollback", lambda conn: _finish(conn.info))
    event.listen(engine.sync_engine.pool, "checkin", lambda _dbapi, record: _finish(record.info))


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        is_sqlite = url.startswith("sqlite")
        # pool_pre_ping is meaningful for networked DBs; pointless for a local file.
        _engine = create_async_engine(url, pool_pre_ping=not is_sqlite)
        if is_sqlite:
            _configure_sqlite(_engine)
            _watch_writes(_engine)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session: commit on success, rollback on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
