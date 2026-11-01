"""Shared test fixtures: an isolated, fully-migrated SQLite DB per test.

Each test gets a fresh on-disk SQLite (schema via Base.metadata plus the FTS5 search
index, which lives in a migration rather than in the models), and an isolated data dir so
attachment/cache code never touches the real one. FK enforcement matches production.
"""

from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from email_reader.models import Base
from email_reader.search.index import DDL as _SEARCH_DDL

# The suite must not lean on the machine it runs on: a model key from the checkout's .env
# decided whether meetings get routed, and fixtures assume UTC. Every model call is stubbed.
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["TZ"] = "UTC"
time.tzset()


@pytest.fixture
async def db(tmp_path):
    os.environ["EMAIL_READER_DATA_DIR"] = str(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
        for stmt in _SEARCH_DDL:
            await c.exec_driver_sql(stmt)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# --- Spaces (docs/spaces.md) -----------------------------------------------------------
# Every mailbox, project, meeting and item lives in a space (NOT NULL). Tests that build
# rows by hand stamp SPACE_ID / SPACE_ID_2 and call mk_space() once per session; fixed
# ids keep the stamping a constant instead of threading a variable through every helper.
import uuid as _uuid  # noqa: E402

from email_reader.models import Space  # noqa: E402

SPACE_ID = _uuid.UUID(int=1)
SPACE_ID_2 = _uuid.UUID(int=2)


async def mk_space(s, n: int = 1, name: str | None = None) -> Space:
    """The test space with the fixed id; created on first call, reused after."""
    sid = _uuid.UUID(int=n)
    sp = await s.get(Space, sid)
    if sp is None:
        sp = Space(id=sid, name=name or f"Пространство {n}", sort=n)
        s.add(sp)
        await s.flush()
        # Как в create_space: у пространства с самого начала есть доски.
        from email_reader import boards as boards_svc

        await boards_svc.seed(s, sp.id)
    return sp
