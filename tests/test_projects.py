from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from email_reader import model_connections as mc
from email_reader import projects as projects_svc
from email_reader import prompts
from email_reader.ai import extractor as projects_svc_extractor
from email_reader.ai.extractor import Usage
from email_reader.ai.schemas import ProjectMatchResult
from email_reader.enums import ActionStatus, ActionType, Direction
from email_reader.model_connections import ResolvedModel
from email_reader.models import Account, ActionItem, Email, Project, ProjectThread, Thread
from tests.conftest import SPACE_ID, mk_space


async def _seed_thread(s):
    now = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    t = Thread(last_at=now)
    s.add(t)
    await s.flush()
    e = Email(direction="inbound", from_addr="noreply@check.yandex.ru", from_name="Чек",
              to_addrs=[], cc_addrs=[], subject="Электронный чек", body_text="598 ₽",
              sent_at=now, thread_id=t.id, dedup_hash=str(uuid.uuid4()))
    s.add(e)
    await s.flush()
    return t


async def test_unassign_keeps_exported_folder(db, tmp_path):
    """New contract: the project folder is additive. Removing a thread from a project drops
    only the membership row — exported files stay on disk, never auto-deleted."""
    folder = tmp_path / "proj"
    async with db() as s:
        await mk_space(s)
        p = await projects_svc.create_project(
            s, space_id=SPACE_ID, name="продажа авто", folder_path=str(folder),
            prompt="всё про авто"
        )
        t = await _seed_thread(s)
        await projects_svc.assign(s, p.id, t.id, source="manual")
        thread = await s.get(Thread, t.id)
        await projects_svc.export_thread_to_project(s, p, thread)
        await s.commit()
        pid, tid = p.id, t.id

    # Threads live under «Переписка/» now — the project root is shared with the meeting
    # notes and the generated summaries.
    mail = folder / "Переписка"
    assert list(mail.glob("Электронный чек-*")), "assign+export should create the folder"

    async with db() as s:

        await mk_space(s)
        await projects_svc.unassign(s, pid, tid)
        await s.commit()
        assert await projects_svc.projects_for_thread(s, tid) == []

    assert list(mail.glob("Электронный чек-*")), "unassign must NOT delete exported files"


async def test_export_all_threads_is_additive(db, tmp_path):
    """export_all_threads_for_project writes every assigned thread and never deletes.
    A stray file placed by the user survives the export."""
    folder = tmp_path / "proj3"
    async with db() as s:
        await mk_space(s)
        p = await projects_svc.create_project(
            s, space_id=SPACE_ID, name="p3", folder_path=str(folder), prompt="x"
        )
        t = await _seed_thread(s)
        await projects_svc.assign(s, p.id, t.id, source="manual")
        await s.commit()
        # A user-added file that export must not touch.
        stray = folder / "мои-заметки.txt"
        stray.write_text("важное", encoding="utf-8")

        n = await projects_svc.export_all_threads_for_project(s, p)
        await s.commit()

    assert n == 1
    assert list((folder / "Переписка").glob("Электронный чек-*")), (
        "export must write the assigned thread"
    )
    assert stray.exists(), "export must not delete user files"


async def test_unassign_survives_missing_folder(db, tmp_path):
    """unassign is best-effort: a never-exported / already-deleted folder is not an error."""
    folder = tmp_path / "proj2"
    async with db() as s:
        await mk_space(s)
        p = await projects_svc.create_project(
            s, space_id=SPACE_ID, name="p2", folder_path=str(folder), prompt="x"
        )
        t = await _seed_thread(s)
        await projects_svc.assign(s, p.id, t.id, source="manual")  # no export
        await s.commit()
        await projects_svc.unassign(s, p.id, t.id)  # must not raise
        await s.commit()
        assert await projects_svc.projects_for_thread(s, t.id) == []

async def test_the_model_is_awaited_with_the_database_free_to_write(db, tmp_path, monkeypatch):
    """docs/feature-storage-plan.md, этап 1: пока модель думает, база должна быть свободна."""
    seen: dict = {}

    async def probing_classify(*a, **kw):
        other = sqlite3.connect(db.kw["bind"].url.database, timeout=0.2)
        try:
            other.execute("CREATE TABLE IF NOT EXISTS probe (x INTEGER)")
            other.commit()
            seen["other_writer"] = "ok"
        except sqlite3.OperationalError as exc:
            seen["other_writer"] = str(exc)
        finally:
            other.close()
        return ProjectMatchResult(matches=[]), Usage(1, 1)

    async def fake_resolve(*a, **k):
        return ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")

    monkeypatch.setattr(projects_svc_extractor, "classify_projects", probing_classify)
    monkeypatch.setattr(mc, "resolve_for_space", fake_resolve)

    async with db() as s:
        await mk_space(s)
        acc = await _account(s)
        await projects_svc.create_project(
            s, space_id=SPACE_ID, name="чеки", folder_path=str(tmp_path / "p"), prompt="про чеки",
        )
        t = await _seed_thread(s)
        t.account_id = acc.id
        await s.commit()  # воркер зовёт обработчик с чистой сессией — подготовка не в счёт

        await projects_svc.process_project_match(s, {"thread_id": str(t.id)})
        await s.commit()

    assert seen["other_writer"] == "ok"


# --- Конспект переписки (промпт mail_summary; пусто = выключено) ---


def _stub_digest(monkeypatch, text="Договорились по чеку.", *, fails=False):
    """Подменяем модель и считаем вызовы: цена этой фичи измеряется ровно в них."""
    calls: dict = {"n": 0}

    async def fake_complete(**kw):
        calls["n"] += 1
        if fails:
            raise RuntimeError("модель недоступна")
        calls["system"] = kw["system"]
        calls["user"] = kw["user"]
        return text

    async def fake_resolve(*a, **k):
        return ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")

    monkeypatch.setattr(projects_svc_extractor, "complete_text", fake_complete)
    monkeypatch.setattr(mc, "resolve_for_space", fake_resolve)
    return calls


async def _account(s):
    a = Account(
        space_id=SPACE_ID, name="Работа", provider="yandex", host="h", port=993, login="w@y.ru",
                password_enc="x", folders=["INBOX"], enabled=True)
    s.add(a)
    await s.flush()
    return a


async def _export(s, folder, *, account=None, thread=None):
    p = await projects_svc.create_project(
        s, space_id=SPACE_ID, name="чеки", folder_path=str(folder), prompt="про чеки",
    )
    t = thread or await _seed_thread(s)
    await projects_svc.export_thread_to_project(s, p, await s.get(Thread, t.id))
    return next((folder / "Переписка").iterdir())


async def test_without_the_prompt_letters_are_copied_as_is(db, tmp_path, monkeypatch):
    """Значение по умолчанию — пусто. Модель не зовём вообще: это и есть «как есть»."""
    calls = _stub_digest(monkeypatch)
    async with db() as s:
        await mk_space(s)
        box = await _export(s, tmp_path / "proj", account=await _account(s))
    assert not (box / projects_svc.SUMMARY_FILE).exists()
    assert list(box.glob("01-*.md")), "письмо должно лежать как есть"
    assert calls["n"] == 0


async def test_the_prompt_replaces_the_letters_with_one_digest(db, tmp_path, monkeypatch):
    calls = _stub_digest(monkeypatch)
    async with db() as s:
        await mk_space(s)
        acc = await _account(s)
        await prompts.set_prompt(s, "mail_summary", acc.space_id, "СЖИМАЙ")
        box = await _export(s, tmp_path / "proj", account=acc)

    summary = box / projects_svc.SUMMARY_FILE
    text = summary.read_text(encoding="utf-8")
    assert "Договорились по чеку." in text
    assert "формируется автоматически" in text, "сгенерированный файл обязан это говорить"
    assert not list(box.glob("01-*.md")), "конспект ЗАМЕНЯЕТ письма, а не дополняет их"
    assert calls["n"] == 1 and calls["system"] == "СЖИМАЙ"
    assert "598 ₽" in calls["user"], "в модель уходит текст писем треда"


async def test_reexport_of_an_unchanged_thread_costs_no_model_call(
    db, tmp_path, monkeypatch
):
    """«Выгрузить всё» перезапускает КАЖДЫЙ тред проекта одним нажатием — без
    водяного знака это N вызовов модели на каждое нажатие."""
    calls = _stub_digest(monkeypatch)
    async with db() as s:
        await mk_space(s)
        acc = await _account(s)
        await prompts.set_prompt(s, "mail_summary", acc.space_id, "СЖИМАЙ")
        folder = tmp_path / "proj"
        p = await projects_svc.create_project(
            s, space_id=SPACE_ID, name="чеки", folder_path=str(folder), prompt="про чеки",
        )
        t = await _seed_thread(s)
        for _ in range(3):
            await projects_svc.export_thread_to_project(s, p, await s.get(Thread, t.id))
        assert calls["n"] == 1

        # Пришло новое письмо — знак сдвинулся, конспект пересобирается.
        s.add(Email(direction="inbound", from_addr="b@x.ru", from_name="Б", to_addrs=[],
                    cc_addrs=[], subject="Электронный чек", body_text="ещё 100 ₽",
                    sent_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC), thread_id=t.id,
                    dedup_hash=str(uuid.uuid4())))
        await s.flush()
        await projects_svc.export_thread_to_project(s, p, await s.get(Thread, t.id))
        assert calls["n"] == 2


async def test_a_dead_model_falls_back_to_copying_the_letters(db, tmp_path, monkeypatch):
    """Экспорт вызывается из UI-действий и из воркера: недоступность модели не имеет
    права уронить раскладку треда по проекту."""
    calls = _stub_digest(monkeypatch, fails=True)
    async with db() as s:
        await mk_space(s)
        acc = await _account(s)
        await prompts.set_prompt(s, "mail_summary", acc.space_id, "СЖИМАЙ")
        box = await _export(s, tmp_path / "proj", account=acc)

    # Именно ПОСЛЕ неудачной попытки, а не потому, что промпт не подхватился.
    assert calls["n"] == 1
    assert not (box / projects_svc.SUMMARY_FILE).exists()
    assert list(box.glob("01-*.md")), "упала модель — письма всё равно на месте"


# --- Проекты без движения (docs/feature-card-advice.md §1.8) -------------------------


async def _stalled_setup(s, now):
    """Проект с открытым обязательством и старым письмом, проект с тем же письмом, но
    свежей встречей, и проект без открытых обязательств."""
    from email_reader.models import Meeting, ProjectMeeting

    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@b.ru", folders=[])
    s.add(acc)
    await s.flush()
    made = {}
    for name in ("Замерший", "Живой", "Пустой"):
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()
        p = Project(space_id=SPACE_ID, name=name, prompt="")
        s.add(p)
        await s.flush()
        s.add(ProjectThread(project_id=p.id, thread_id=th.id, source="manual"))
        s.add(Email(account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
                    direction=Direction.INBOUND, from_addr="x@y.z", to_addrs=[], cc_addrs=[],
                    sent_at=now - timedelta(days=40)))
        if name != "Пустой":
            s.add(ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title=f"К {name}",
                             status=ActionStatus.OPEN, source_thread_id=th.id,
                             updated_at=now - timedelta(days=40)))
        made[name] = p
    m = Meeting(ext_id="m-live", space_id=SPACE_ID, started_at=now - timedelta(days=2),
                title="Синк")
    s.add(m)
    await s.flush()
    s.add(ProjectMeeting(project_id=made["Живой"].id, meeting_id=m.id, source="manual"))
    await s.commit()
    return made


async def test_stalled_lists_only_projects_with_open_work_and_no_movement(db):
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    async with db() as s:
        made = await _stalled_setup(s, now)
        rows = await projects_svc.stalled(s, SPACE_ID, 14, now)
    # «Живой» двигался (встреча 2 дня назад), «Пустой» ничего не должен — не замерли.
    assert [(r.name, r.days, r.open_cards) for r in rows] == [("Замерший", 40, 1)]
    assert rows[0].project_id == made["Замерший"].id


async def test_stalled_respects_the_threshold_and_a_touched_card(db):
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    async with db() as s:
        made = await _stalled_setup(s, now)
        assert await projects_svc.stalled(s, SPACE_ID, 60, now) == []  # порог выше тишины
        # Тронул карточку — движение есть, проект больше не замерший.
        item = (await s.execute(
            select(ActionItem).where(ActionItem.title == "К Замерший")
        )).scalar_one()
        item.updated_at = now - timedelta(days=1)
        await s.commit()
        assert await projects_svc.stalled(s, SPACE_ID, 14, now) == []
        # Выключенный проект не считается вовсе.
        item.updated_at = now - timedelta(days=40)
        made["Замерший"].enabled = False
        await s.commit()
        assert await projects_svc.stalled(s, SPACE_ID, 14, now) == []


async def test_a_thread_name_is_unique_per_space_not_globally(db):
    """«Договор» может быть и рабочей, и личной Нитью; в одном пространстве — одна."""
    from sqlalchemy.exc import IntegrityError

    from tests.conftest import SPACE_ID_2

    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        s.add_all([Project(space_id=SPACE_ID, name="Договор", prompt=""),
                   Project(space_id=SPACE_ID_2, name="Договор", prompt="")])
        await s.commit()
        s.add(Project(space_id=SPACE_ID, name="Договор", prompt=""))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
        else:
            raise AssertionError("second «Договор» in one space must be refused")
        names = (await s.execute(
            select(Project.space_id).where(Project.name == "Договор")
        )).scalars().all()
    assert sorted(names) == sorted([SPACE_ID, SPACE_ID_2])
