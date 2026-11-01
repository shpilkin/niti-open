"""Entry point: run the system's processes.

Subcommands:
  migrate    apply DB migrations (alembic upgrade head)
  sync       poll mailboxes -> store + enqueue
  worker     process events (pipeline + extraction + commitment) + scheduler
  api        web UI / read API (uvicorn)
  bot        Telegram bot
  all        sync + worker + scheduler + bot + api in one process (personal use)
  reindex    rebuild the search index from the store (takes a while on a big mailbox)
  selfcheck  import critical deps + open the DB; exit 1 on failure. Run against the
             frozen binary after packaging — catches PyInstaller gaps (lazy imports
             like caldav's PEP 562) that only break in the .app, never in dev.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


def _alembic_config():
    """Alembic Config that works both in-repo and inside a PyInstaller bundle.

    The DB URL comes from app settings (see alembic/env.py); we only need to
    point alembic at the migration scripts, which live under the resource dir.
    """
    from alembic.config import Config

    from email_reader.paths import resource_path

    cfg = Config()
    cfg.set_main_option("script_location", str(resource_path("alembic")))
    return cfg


def _migrate() -> None:
    from alembic import command

    command.upgrade(_alembic_config(), "head")


async def _run_scheduler(stop: asyncio.Event) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from email_reader.commitment.scheduler import tick
    from email_reader.db import session_scope

    async def job() -> None:
        async with session_scope() as session:
            overdue, followups = await tick(session)
        if overdue or followups:
            logging.getLogger("email_reader.scheduler").info(
                "tick: %d overdue, %d follow-ups", overdue, followups
            )

    async def calendar_job() -> None:
        from datetime import UTC, datetime

        from email_reader import calendar_sync

        summary = await calendar_sync.sync_all_accounts(datetime.now(UTC))
        if summary:
            logging.getLogger("email_reader.scheduler").info("calendar sync: %s", summary)

    async def meetings_job() -> None:
        from email_reader import meetings

        stats = await meetings.scan_inbox()
        if stats.get("ingested") or stats.get("rejected"):
            logging.getLogger("email_reader.scheduler").info("meetings inbox: %s", stats)

    async def text_repair_job() -> None:
        from email_reader.pipeline import text_repair

        for name, run in (
            ("html", text_repair.repair_html_bodies),
            ("base64", text_repair.repair_base64_bodies),
        ):
            async with session_scope() as session:
                stats = await run(session)
            if stats["rebuilt"]:
                logging.getLogger("email_reader.scheduler").info(
                    "text repair (%s): rebuilt %d bodies of %d scanned",
                    name, stats["rebuilt"], stats["scanned"],
                )

    async def counterparty_backfill_job() -> None:
        from email_reader.commitment import engine as commitment_engine

        async with session_scope() as session:
            stats = await commitment_engine.backfill_counterparties(session)
        if stats["named"]:
            logging.getLogger("email_reader.scheduler").info(
                "counterparty backfill: named %d of %d cards", stats["named"], stats["scanned"]
            )

    async def boards_seed_job() -> None:
        """Пространство без досок — пустое меню. Пресеты заводит миграция и create_space, но
        проход идемпотентен и страхует пространство, попавшее в базу иным путём."""
        from email_reader import boards as boards_svc

        async with session_scope() as session:
            added = await boards_svc.seed_all(session)
        if added:
            logging.getLogger("email_reader.scheduler").info(
                "boards: seeded %d preset boards", added
            )

    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    scheduler = AsyncIOScheduler()
    scheduler.add_job(job, "interval", minutes=30, id="tick")
    # The recorder app hands meetings over through a folder, so the only way to notice a
    # new one is to look. A minute is well inside «доехало, пока я закрывал ноутбук».
    scheduler.add_job(
        meetings_job, "interval", minutes=1, id="meetings", next_run_time=_dt.now(_UTC)
    )
    # next_run_time=now: the first calendar sync fires at startup, not 30 minutes in —
    # otherwise a fresh install shows an empty «К подготовке» for half an hour.
    scheduler.add_job(
        calendar_job, "interval", minutes=30, id="calendar", next_run_time=_dt.now(_UTC)
    )
    # One pass over mail whose text the old HTML extraction shredded. It marks itself
    # done, so the interval is only a retry after a quit in the middle of the pass.
    scheduler.add_job(
        text_repair_job, "interval", hours=24, id="text-repair", next_run_time=_dt.now(_UTC)
    )
    # Same shape: one pass that names the cards saved with an empty counterparty before
    # the thread rule existed; marks itself done.
    scheduler.add_job(
        counterparty_backfill_job, "interval", hours=24, id="counterparty-backfill",
        next_run_time=_dt.now(_UTC),
    )
    scheduler.add_job(
        boards_seed_job, "interval", hours=24, id="boards-seed", next_run_time=_dt.now(_UTC),
    )
    scheduler.start()
    await stop.wait()
    scheduler.shutdown(wait=False)


def _run_api() -> None:
    import uvicorn

    from email_reader.config import get_settings

    s = get_settings()
    uvicorn.run("email_reader.api.app:app", host=s.web_host, port=s.web_port, log_level="info")


async def _api_task() -> None:
    import uvicorn

    from email_reader.config import get_settings

    s = get_settings()
    config = uvicorn.Config(
        "email_reader.api.app:app", host=s.web_host, port=s.web_port, log_level="info"
    )
    await uvicorn.Server(config).serve()


async def _supervise(name: str, factory, stop: asyncio.Event) -> None:
    """Keep a background task alive: if it crashes, log and restart it. This isolates
    background failures (worker/sync/bot) from the web server, so a transient DB lock
    or a bug in one loop can never take the whole backend down ("Internal Server
    Error" in the desktop window)."""
    log = logging.getLogger("email_reader.cli")
    while not stop.is_set():
        try:
            await factory()
            return  # clean exit (e.g. stop requested)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s crashed; restarting in 3s", name)
            await asyncio.sleep(3)


async def _run_all() -> None:
    from email_reader.connectors.sync_worker import run_sync_loop
    from email_reader.pipeline.worker import run_worker
    from email_reader.telegram.bot import run_bot, run_bots

    stop = asyncio.Event()
    # The API runs directly; background loops are supervised so their crashes never
    # tear down the web server.
    await asyncio.gather(
        _supervise("sync", lambda: run_sync_loop(stop), stop),
        _supervise("worker", lambda: run_worker(stop=stop), stop),
        _supervise("scheduler", lambda: _run_scheduler(stop), stop),
        _supervise("bot", lambda: run_bot(), stop),
        _supervise("bots", lambda: run_bots(stop), stop),
        _api_task(),
    )


def _selfcheck() -> int:
    """Verify the runtime can actually work: import-check modules that PyInstaller may
    miss (dynamic/PEP 562 imports) and open the database. Returns a process exit code."""
    import importlib

    modules = [
        "caldav",
        "caldav.davclient",  # loaded lazily via PEP 562 — invisible to modulegraph
        "icalendar",
        "anthropic",
        "openai",
        "aiogram",
        "apscheduler.schedulers.asyncio",
        "aiosqlite",
        "sqlalchemy.dialects.sqlite",
        # snowballstemmer imports the language module by name at first use — the same
        # dynamic-import shape that made caldav die only inside the .app.
        "snowballstemmer",
        "email_reader.search.index",
        # The MCP endpoint is imported only when it mounts, so a missing transport
        # dependency would disable /mcp in the .app with nothing in the log.
        "mcp.server.mcpserver",
        "email_reader.mcp.server",
        "email_reader.api.app",
    ]
    failed: list[str] = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001 — report every failure, not just the first
            failed.append(f"{mod}: {exc}")

    # Building the server is what pulls the transport in — importing the module is not
    # enough to prove the endpoint will mount.
    try:
        from email_reader.mcp.server import build_server

        build_server()
    except Exception as exc:  # noqa: BLE001
        failed.append(f"mcp server: {exc}")

    async def _db_ping() -> None:
        from sqlalchemy import text

        from email_reader.db import session_scope

        async with session_scope() as s:
            await s.execute(text("SELECT 1"))

    try:
        asyncio.run(_db_ping())
    except Exception as exc:  # noqa: BLE001
        failed.append(f"db: {exc}")

    if failed:
        print("SELFCHECK FAILED:")
        for f in failed:
            print(f"  - {f}")
        return 1
    print("selfcheck ok")
    return 0


async def _reindex() -> None:
    from email_reader.db import session_scope
    from email_reader.search import index as search_index

    await search_index.rebuild(session_scope)
    async with session_scope() as session:
        written = await search_index.counts(session)
    print("search index rebuilt:", ", ".join(f"{k}={n}" for k, n in written.items()))


def main() -> None:
    parser = argparse.ArgumentParser(prog="email-reader")
    parser.add_argument(
        "cmd",
        choices=["migrate", "sync", "worker", "api", "bot", "all", "app", "reindex",
                 "selfcheck"],
    )
    args = parser.parse_args()

    if args.cmd == "selfcheck":
        raise SystemExit(_selfcheck())
    if args.cmd == "migrate":
        _migrate()
        return
    if args.cmd == "reindex":
        asyncio.run(_reindex())
        return
    if args.cmd == "api":
        _run_api()
        return
    if args.cmd == "app":
        # Desktop entry point: ensure schema is current, then run everything.
        _migrate()
        asyncio.run(_run_all())
        return

    if args.cmd == "sync":
        from email_reader.connectors.sync_worker import run_sync_loop

        asyncio.run(run_sync_loop())
    elif args.cmd == "worker":
        async def _worker_and_sched() -> None:
            from email_reader.pipeline.worker import run_worker

            stop = asyncio.Event()
            await asyncio.gather(run_worker(stop=stop), _run_scheduler(stop))

        asyncio.run(_worker_and_sched())
    elif args.cmd == "bot":
        from email_reader.telegram.bot import run_bot

        asyncio.run(run_bot())
    elif args.cmd == "all":
        asyncio.run(_run_all())


if __name__ == "__main__":
    main()
