"""E2E harness: migrate + seed a temp DB, run the real FastAPI app via uvicorn in a
subprocess, and point Playwright at it. Session-scoped so the server starts once.

Run:  pytest -m e2e tests/e2e
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VENV_BIN = Path(sys.executable).parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _seed(data_dir: Path) -> None:
    """Insert two accounts, a few emails (one for search), a commitment and a meeting."""
    import os

    os.environ["EMAIL_READER_DATA_DIR"] = str(data_dir)
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from email_reader.models import (
        Account,
        ActionItem,
        AgentDecision,
        AppSetting,
        Email,
        EmailOccurrence,
        Label,
        Meeting,
        ModelConnection,
        Project,
        ProjectMeeting,
        SenderRule,
        Space,
        Thread,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{data_dir}/email_reader.db")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    uid = iter(range(1, 1000))

    async def email(s, thr, acc, *, direction="inbound", frm="bob@x.com", name="Bob",
                    subject="S", body="B", attach=False, html=None):
        # account_id mirrors production (per-mailbox scoping). attach is intentionally
        # left False: has_attachments=True would auto-load /email/{id}/attachments, which
        # fetches from the (fake) IMAP host and blocks — unrelated to these smoke tests.
        e = Email(account_id=acc.id, direction=direction, from_addr=frm, from_name=name,
                  to_addrs=[], cc_addrs=[], subject=subject, body_text=body, sent_at=now,
                  body_raw_html=html, thread_id=thr.id, dedup_hash=str(uuid.uuid4()),
                  has_attachments=attach)
        s.add(e)
        await s.flush()
        s.add(EmailOccurrence(email_id=e.id, account_id=acc.id, folder="INBOX", uid=next(uid)))
        return e

    async with sm() as s:
        # Two spaces (docs/spaces.md): «Работа» owns the work mailbox, the meetings and
        # the projects; «Дом» owns the home mailbox. Nothing crosses between them.
        sp_work = Space(name="Работа", sort=0)
        sp_home = Space(name="Дом", sort=1)
        s.add_all([sp_work, sp_home])
        await s.flush()
        work = Account(name="Работа", provider="yandex", host="h", port=993,
                       login="work@y.ru", password_enc="x", folders=["INBOX"], enabled=True,
                       space_id=sp_work.id)
        home = Account(name="Дом", provider="gmail", host="h", port=993,
                       login="home@g.com", password_enc="x", folders=["INBOX"], enabled=True,
                       space_id=sp_home.id)
        s.add_all([work, home])
        await s.flush()
        # Подпись с корпоративным шрифтом: письмо уходит им же, и поле ввода должно
        # показывать его, а не системный.
        s.add(AppSetting(key=f"signature:{work.id}",
                         value='<div style="font-family:&quot;Calibri&quot;,sans-serif;'
                               'font-size:11pt">Глеб Миронов</div>'))
        await s.flush()

        tw = Thread(last_at=now, account_id=work.id)
        s.add(tw)
        await s.flush()
        # С разметкой: в карточке обязательства письмо должно читаться так же, как в
        # почте, — плоским текстом таблица рассыпается в столбик.
        await email(s, tw, work, subject="Договор аренды",
                    body="пришлите подписанный договор до пятницы",
                    html="<table><tr><td>Срок</td><td>пятница</td></tr></table>")
        s.add(ActionItem(type="NEED_MY_REPLY", title="ответить по договору",
                         counterparty="Bob", source_thread_id=tw.id, status="open",
                         space_id=sp_work.id))
        # Своя карточка для проверки, что моё закрытие попадает в историю: её снимают
        # с доски, поэтому делить её с другими тестами нельзя. Без контрагента — экран
        # «Люди» собирается из контрагентов карточек, лишний человек ломал бы его проверки.
        s.add(ActionItem(type="TASK", title="Сверить счёт с договором", status="open",
                         space_id=sp_work.id))
        # Залежавшаяся карточка: ассистент предлагает закрыть, решает пользователь.
        # Без контрагента — чтобы не появился лишний человек на экране «Люди».
        s.add(ActionItem(type="TASK", title="Уточнить смету у подрядчика", status="open",
                         space_id=sp_work.id, close_candidate=True, close_hint="idle"))
        # Второе обязательство по той же беседе: на нём проверяется, что письмо в карточке
        # показано разметкой. Отдельное — потому что первое закрывают другие тесты.
        with_past = ActionItem(type="MY_COMMITMENT", title="перечитать письмо с таблицей",
                               counterparty="Bob", source_thread_id=tw.id, status="open",
                               space_id=sp_work.id)
        s.add(with_past)
        await s.flush()
        # Прошлое этой карточки: ассистент сменил тип, потом снял её с доски, потом она
        # вернулась. На ней проверяется, что «снята» и «закрыта» читаются по-разному.
        for n, (kind, payload) in enumerate((
            ("transition", {"from": "WAITING_FOR_REPLY", "to": "MY_COMMITMENT",
                            "reason": "пришёл ответ и новый вопрос"}),
            ("auto_resolve", {"reason": "superseded after thread update"}),
            ("transition", {"action": "reopen"}),
        )):
            s.add(AgentDecision(
                kind=kind, action_item_id=with_past.id, payload=payload,
                created_at=now - timedelta(days=3 - n),
            ))

        # Беседа из двух писем — на ней проверяется группировка по теме и раскрытие
        # стрелкой. Даты старше остальных, чтобы она не встала первой в списке и не
        # сдвинула проверки, которые берут «первую строку».
        old = now - timedelta(days=5)
        tconv = Thread(last_at=old, account_id=work.id)
        s.add(tconv)
        await s.flush()
        for i in range(2):
            e = Email(account_id=work.id, direction="inbound", from_addr="lena@x.com",
                      from_name="Лена", to_addrs=[], cc_addrs=[], subject="Смета на кухню",
                      body_text=f"сообщение {i}", sent_at=old + timedelta(minutes=i),
                      thread_id=tconv.id, dedup_hash=str(uuid.uuid4()))
            s.add(e)
            await s.flush()
            s.add(EmailOccurrence(email_id=e.id, account_id=work.id, folder="INBOX",
                                  uid=next(uid)))

        # Беседа из трёх писем, где правило заведено на одного из отправителей: метка
        # должна стоять на его письме, а не на всей переписке. Дата старее прочих, чтобы
        # беседа не встала первой и не сдвинула проверки, берущие «первую строку».
        older = now - timedelta(days=7)
        tlbl = Thread(last_at=older, account_id=work.id)
        s.add(tlbl)
        await s.flush()
        for i, sender in enumerate(("anton@x.com", "boris@x.com", "anton@x.com")):
            e = Email(account_id=work.id, direction="inbound", from_addr=sender,
                      from_name=sender.split("@")[0], to_addrs=[], cc_addrs=[],
                      subject="Доступы к API", body_text=f"реплика {i}",
                      sent_at=older + timedelta(minutes=i), thread_id=tlbl.id,
                      read_at=older, dedup_hash=str(uuid.uuid4()))
            s.add(e)
            await s.flush()
            s.add(EmailOccurrence(email_id=e.id, account_id=work.id, folder="INBOX",
                                  uid=next(uid)))
        # Карточка из этой же беседы: на ней проверяется, что метка правила видна и на
        # задаче, а не только в почте. Без контрагента — экран «Люди» собирается из них.
        s.add(ActionItem(type="TASK", title="Сверить доступы к API", status="open",
                         source_thread_id=tlbl.id, space_id=sp_work.id))
        lbl_dk = Label(name="БЛ", color="#5e30eb")
        s.add(lbl_dk)
        await s.flush()
        s.add(SenderRule(match_type="address", pattern="boris@x.com",
                         action="label", label_id=lbl_dk.id))
        await s.flush()
        # Метки раскладывает тот же код, что и в приложении, — сид не проставляет их сам.
        from email_reader import boards as boards_svc
        from email_reader import labels as labels_svc

        await labels_svc.apply_rules_to_existing(s)
        # Пространства здесь создаются напрямую, а не через create_space, поэтому доски
        # стенду нужно завести самому — как это делает миграция на боевой базе.
        spaces = [sp_work, sp_home]
        for sp in spaces:
            await boards_svc.seed(s, sp.id)


        th = Thread(last_at=now, account_id=home.id)
        s.add(th)
        await s.flush()
        await email(s, th, home, frm="anna@x.com", name="Анна", subject="Привет",
                    body="как дела")
        # Same person as the meeting task below, but in the OTHER mailbox: his dossier
        # opened from a meeting must not pull this in (per-mailbox isolation).
        await email(s, th, home, frm="ivanov@x.com", name="Иванов", subject="Личное",
                    body="частная переписка не по работе")

        # A meeting plus its transcript on disk: the pane resolves the path through the
        # archive-root setting, so seeding both proves that link end to end.
        asr = data_dir / "ASR"
        (asr / "2026-06").mkdir(parents=True, exist_ok=True)
        (asr / "2026-06" / "rec.txt").write_text(
            "[00:00:04] Спикер 1: Планёрка по крабам.\n"
            "[00:00:20] Спикер 2: Смету пришлю в пятницу.\n",
            encoding="utf-8",
        )
        s.add(AppSetting(key="meetings.asr_root", value=str(asr)))
        # New meetings land in «Работа».
        s.add(AppSetting(key="meetings.space_id", value=str(sp_work.id)))
        # Разобранная: есть проект, саммари и предложение — значит routed_at обязан
        # стоять, иначе экран считает её необработанной.
        meeting = Meeting(ext_id="e2e0001", started_at=now, duration_sec=1800,
                          space_id=sp_work.id,
                          title="Планёрка по крабам", participants=[],
                          summary="Обсудили сроки поставки и смету.",
                          transcript_path="2026-06/rec.txt", source="e2e",
                          routed_at=now)
        supply = Project(name="Поставки", folder_path=str(data_dir / "П-поставки"),
                         prompt="всё про поставки", include_meetings=True, aliases=[],
                         space_id=sp_work.id)
        estimate = Project(name="Смета", folder_path=str(data_dir / "П-смета"),
                           prompt="сметы и цены", include_meetings=True, aliases=[],
                           space_id=sp_work.id)
        s.add_all([meeting, supply, estimate])
        await s.flush()
        # Routed into one project, with the second one still only proposed — the state
        # the «спорная зона» UI is for.
        s.add(ProjectMeeting(project_id=supply.id, meeting_id=meeting.id, source="auto",
                             confidence=0.9, reason="про сроки поставки"))
        meeting.suggestions = [{"project_id": str(estimate.id), "name": "Смета",
                                "confidence": 0.62, "reason": "упоминалась смета"}]
        # Due-dated so it lands on «Сегодня»: a task spoken at a meeting has to be a
        # first-class board citizen, and that is what the board-side tests check.
        s.add(ActionItem(type="DELEGATED_TASK", title="Подготовить смету",
                         counterparty="Иванов", source_meeting_id=meeting.id,
                         status="open", due_date=now.date(), space_id=sp_work.id,
                         reason="[00:23:10] сказано на встрече"))
        # Тот же человек, что «Иванов» со встречи, но пришедший из почты и с адресом —
        # ради экрана «Люди»: две строки об одном человеке, которые надо объединить.
        # Закрытая карточка: в списке людей она видна, ни на одну живую доску не попадает.
        s.add(ActionItem(type="MY_COMMITMENT", title="прислать смету", status="done",
                         counterparty="И. Иванов (ivanov@krab.ru)", resolved_by="user",
                         space_id=sp_work.id, source_thread_id=tw.id))
        # Длинная «Делегировано»: доска не должна прыгать наверх, когда закрываешь
        # карточку в её середине. Без срока и без контрагента — ни на «Сегодня», ни в «Люди».
        for n in range(40):
            s.add(ActionItem(type="DELEGATED_TASK", title=f"Поручение {n:02d}", status="open",
                             space_id=sp_work.id, reason="для прокрутки доски"))
        # A second row on «Сегодня» that no test resolves: the toolbar tests check that
        # rows keep their place, and that takes two of them.
        s.add(ActionItem(type="MY_COMMITMENT", title="Согласовать график поставок",
                         counterparty="Анна Соколова", status="open", due_date=now.date(),
                         space_id=sp_work.id, reason="обещал в письме"))
        # Модель настроена (ключ фиктивный, воркер в e2e не запущен и никуда не ходит):
        # без неё разбор невозможен и кнопка не покажется.
        from email_reader.crypto import encrypt

        s.add(ModelConnection(name="test", provider="openai", kind="openai",
                              base_url="http://127.0.0.1:9", api_key_enc=encrypt("k"),
                              model="m", is_default=True, enabled=True))

        # Вторая запись, которую разобрать не удалось: событие для очереди «выполнено»,
        # само оно не повторится. Ради неё на экране и появляется кнопка.
        s.add(Meeting(ext_id="e2e0002", space_id=sp_work.id,
                      started_at=now - timedelta(days=1), duration_sec=600,
                      title="Не разобранная планёрка", participants=[],
                      # Файла на диске нет: архив переехал. Скачивание обязано объяснить,
                      # а не отдать пустоту.
                      transcript_path="2026-06/уехал.txt", source="e2e",
                      route_note="Не настроена модель для разбора встреч."))
        # Строки сид пишет мимо приложения, поэтому и поисковый индекс собирает сам.
        from email_reader.search import index as search_index

        await search_index.reindex(s)
        await s.commit()
    await engine.dispose()


@pytest.fixture(autouse=True)
def _count_htmx_swaps(page):
    """Count finished htmx swaps PER TARGET, so a test can wait for the real signal.

    Clicking content htmx has not finished processing does nothing at all — no request,
    no error, the page just sits there. `networkidle` does not cover it: the response is
    already in while htmx still has its settle step to run. Counting per target matters
    because the sidebar swaps itself on load and on every countsChanged — a global
    counter is satisfied by those and lets the click through too early anyway."""
    page.add_init_script(
        "window.__settled = {};"
        "document.addEventListener('htmx:afterSettle', (e) => {"
        "  const t = e.detail && e.detail.target;"
        "  const k = t && t.id ? '#' + t.id : '?';"
        "  window.__settled[k] = (window.__settled[k] || 0) + 1;"
        "});"
    )


@pytest.fixture
def click_and_settle(page):
    """Click, then wait until htmx has settled the swap into `target`."""

    def _click(locator, target: str = "#pane"):
        expr = f"() => (window.__settled && window.__settled['{target}']) || 0"
        n = page.evaluate(expr)
        locator.click()
        page.wait_for_function(f"{expr.replace('() =>', '() =>')} > {n}")

    return _click


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("e2e-data")
    env_path = str(data_dir)
    # 1) migrate to head (real schema + FTS)
    subprocess.run(
        [str(VENV_BIN / "alembic"), "upgrade", "head"],
        cwd=REPO, env={**_env(env_path)}, check=True, capture_output=True,
    )
    # 2) seed
    asyncio.run(_seed(data_dir))
    # 3) launch uvicorn against the temp DB
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "email_reader.api.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env={**_env(env_path)},
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_healthy(f"{base}/view/today")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _env(data_dir: str) -> dict:
    import os
    return {**os.environ, "EMAIL_READER_DATA_DIR": data_dir}


def _wait_healthy(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    raise RuntimeError(f"server did not become healthy at {url}")


@pytest.fixture(scope="session")
def base_url(live_server):  # consumed by pytest-playwright for page.goto("/")
    return live_server
