"""Meeting routing (v1): deterministic rules, confidence zones, and item idempotency.

The model is stubbed everywhere — what is under test is the decision logic around it:
which projects a meeting is filed into, what is only proposed, what is written for items
the user has already ruled on, and what happens when the feature isn't configured.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from email_reader import meetings, settings_store, views
from email_reader import projects as projects_svc
from email_reader.ai import extractor
from email_reader.ai.extractor import Usage
from email_reader.ai.schemas import MeetingItem, MeetingMatch, MeetingRoutingResult
from email_reader.enums import ActionStatus, ActionType
from email_reader.model_connections import ResolvedModel
from email_reader.models import (
    Account,
    ActionItem,
    Meeting,
    ModelConnection,
    Project,
    ProjectMeeting,
)
from tests.conftest import SPACE_ID, mk_space

STARTED = "2026-08-16T14:30:05+03:00"
TRANSCRIPT = (
    "[00:00:04] Спикер 1: Планёрка по поставкам, обсуждаем сроки.\n"
    "[00:23:10] Спикер 2: Смету подготовит Иванов к пятнице.\n"
    "[00:31:40] Спикер 1: Поставщика меняем на второго.\n"
)


async def _account(s, name="Работа", login="work@y.ru") -> Account:
    a = Account(space_id=SPACE_ID, name=name, provider="yandex", host="h", port=993, login=login,
                password_enc="x", folders=["INBOX"], enabled=True)
    s.add(a)
    await s.flush()
    return a


async def _project(s, name, *, aliases=None, include=True, prompt="описание") -> Project:
    p = Project(space_id=SPACE_ID, name=name, folder_path=f"/tmp/{name}", prompt=prompt,
                include_meetings=include, aliases=aliases or [])
    s.add(p)
    await s.flush()
    return p


async def _meeting(s, tmp_path, text: str = TRANSCRIPT):
    rel = "ASROutput/2026-08/rec.txt"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return await meetings.ingest_payload(s, {
        "meeting_id": "meet0001", "started_at": STARTED, "duration_sec": 1800,
        "asr_root": str(tmp_path), "transcript_path": rel, "source": "test",
    })


def _stub(monkeypatch, *, matches=(), items=(), summary="о чём говорили", title=None):
    """Stub the two model calls; record that they happened."""
    calls: dict = {"fold": 0, "route": 0, "hints": None}

    async def fake_fold(text, **kw):
        calls["fold"] += 1
        return text, 0

    async def fake_route(fold, projects, today, hints=None, **kw):
        calls["route"] += 1
        calls["hints"] = hints
        return MeetingRoutingResult(
            title=title, summary=summary, items=list(items), matches=list(matches)
        ), Usage(1, 1)

    async def fake_resolve(*a, **k):
        return ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")

    from email_reader import model_connections as mc

    monkeypatch.setattr(extractor, "fold_transcript", fake_fold)
    monkeypatch.setattr(extractor, "route_meeting", fake_route)
    monkeypatch.setattr(mc, "resolve_for_account", fake_resolve)
    return calls


async def _members(s, meeting_id) -> dict[uuid.UUID, ProjectMeeting]:
    rows = (
        await s.execute(select(ProjectMeeting).where(ProjectMeeting.meeting_id == meeting_id))
    ).scalars().all()
    return {r.project_id: r for r in rows}


async def _drain(s):
    """Отметить события разобранными — это делает воркер, а тесты зовут обработчик
    напрямую. Без этого встреча выглядит «уже в очереди»."""
    from email_reader.enums import EventStatus
    from email_reader.models import Event

    for e in (await s.execute(select(Event).where(Event.status == EventStatus.PENDING))).scalars():
        e.status = EventStatus.DONE
    await s.flush()


async def _items(s, meeting_id) -> list[ActionItem]:
    return list(
        (
            await s.execute(
                select(ActionItem).where(ActionItem.source_meeting_id == meeting_id)
            )
        ).scalars().all()
    )


# --- deterministic rules (§5) ---


def test_marker_needs_the_opening_seconds_but_alias_counts_anywhere():
    class P:
        def __init__(self, name, aliases=()):
            self.id, self.name, self.aliases = uuid.uuid4(), name, list(aliases)

    late = "…" * 2000 + " обсудили проект Крабы в самом конце"
    crabs = P("Крабы")
    assert crabs.id not in meetings.deterministic_matches(late, [crabs])
    assert crabs.id in meetings.deterministic_matches("Это по проекту Крабы, поехали", [crabs])

    # An alias is a rule the user wrote deliberately -> it fires wherever it is said.
    smeta = P("Смета", aliases=["Иванов"])
    assert smeta.id in meetings.deterministic_matches(TRANSCRIPT, [smeta])
    # …but only as a whole word: «Ивановский» is not «Иванов».
    assert smeta.id not in meetings.deterministic_matches("Ивановский район", [smeta])


async def test_marker_wins_over_a_model_that_says_no(db, tmp_path, monkeypatch):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        crabs = await _project(s, "Крабы")
        m = await _meeting(s, tmp_path, "Это по проекту Крабы.\n" + TRANSCRIPT)
        calls = _stub(monkeypatch, matches=[
            MeetingMatch(project="Крабы", member=False, confidence=0.1, reason="нет"),
        ])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()

        row = (await _members(s, m.id))[crabs.id]
        assert row.source == "marker"
        assert "начале записи" in row.reason
        assert calls["hints"] == ["Крабы"]  # the model is told, it just can't overrule


async def test_the_model_is_awaited_with_the_database_free_to_write(db, tmp_path, monkeypatch):
    """docs/feature-storage-plan.md, этап 1: пока модель думает, база должна быть свободна."""
    seen: dict = {}

    async def probing_fold(text, **kw):
        other = sqlite3.connect(db.kw["bind"].url.database, timeout=0.2)
        try:
            other.execute("CREATE TABLE IF NOT EXISTS probe (x INTEGER)")
            other.commit()
            seen["other_writer"] = "ok"
        except sqlite3.OperationalError as exc:
            seen["other_writer"] = str(exc)
        finally:
            other.close()
        return text, 0

    async with db() as s:
        await mk_space(s)
        await _account(s)
        await _project(s, "Крабы")
        m = await _meeting(s, tmp_path, TRANSCRIPT)
        await s.commit()  # воркер зовёт обработчик с чистой сессией — подготовка не в счёт
        _stub(monkeypatch)  # без своего заголовка: заглушка из расшифровки должна уцелеть
        monkeypatch.setattr(extractor, "fold_transcript", probing_fold)
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        stored = await s.get(Meeting, m.id)

    assert seen["other_writer"] == "ok"
    assert stored.title == meetings.transcript_title(TRANSCRIPT)


# --- confidence zones (§7) ---


async def test_confident_files_the_meeting_while_the_grey_zone_only_suggests(
    db, tmp_path, monkeypatch
):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        sure = await _project(s, "Поставки")
        maybe = await _project(s, "Смета")
        weak = await _project(s, "Найм")
        m = await _meeting(s, tmp_path)
        _stub(monkeypatch, matches=[
            MeetingMatch(project="Поставки", member=True, confidence=0.9, reason="про сроки"),
            MeetingMatch(project="Смета", member=True, confidence=0.65, reason="возможно"),
            MeetingMatch(project="Найм", member=True, confidence=0.2, reason="вряд ли"),
        ])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()

        members = await _members(s, m.id)
        assert list(members) == [sure.id]
        assert members[sure.id].source == "auto"
        # The grey zone proposes and writes nothing; the weak one isn't even proposed.
        assert [x["project_id"] for x in m.suggestions] == [str(maybe.id)]
        assert weak.id not in members
        assert m.route_note is None and m.routed_at is not None
        assert m.summary == "о чём говорили"


async def test_no_more_than_three_projects_without_a_human(db, tmp_path, monkeypatch):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        names = ["A", "B", "C", "D"]
        for n in names:
            await _project(s, n)
        m = await _meeting(s, tmp_path)
        _stub(monkeypatch, matches=[
            MeetingMatch(project=n, member=True, confidence=0.95 - i / 100, reason="да")
            for i, n in enumerate(names)
        ])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert len(await _members(s, m.id)) == meetings.MAX_AUTO_PROJECTS


async def test_manual_membership_survives_reclassification(db, tmp_path, monkeypatch):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        mine = await _project(s, "Мой проект")
        m = await _meeting(s, tmp_path)
        await projects_svc.assign_meeting(s, mine.id, m.id, source="manual")
        _stub(monkeypatch, matches=[
            MeetingMatch(project="Мой проект", member=False, confidence=0.0, reason="нет"),
        ])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert (await _members(s, m.id))[mine.id].source == "manual"


# --- which model reads meetings ---


async def _connection(s, name="Своя модель", model="m-explicit", enabled=True,
                      is_default=False):
    from email_reader.crypto import encrypt

    c = ModelConnection(name=name, provider="openai", kind="openai",
                        base_url="https://example/v1", api_key_enc=encrypt("k"),
                        model=model, enabled=enabled, is_default=is_default)
    s.add(c)
    await s.flush()
    return c


async def test_the_model_picked_for_meetings_wins_over_the_space_one(db):
    """Экран «Встречи» выбирает модель напрямую: пространство остаётся владельцем задач,
    но больше не решает, чем читать запись. (Переписано под пространства: модель
    переехала с ящика на пространство.)"""
    async with db() as s:
        space = await mk_space(s)
        await _account(s)
        space_model = await _connection(s, name="Модель пространства", model="m-space")
        space.model_connection_id = space_model.id
        chosen = await _connection(s)
        await settings_store.set_value(s, meetings.SPACE_KEY, str(space.id))
        await settings_store.set_value(s, meetings.MODEL_KEY, str(chosen.id))

        assert (await meetings.routing_model(s)).model == "m-explicit"


async def test_without_a_choice_meetings_keep_using_the_space_model(db):
    async with db() as s:
        space = await mk_space(s)
        await _account(s)
        await _connection(s, name="Общая по умолчанию", model="m-global", is_default=True)
        space_model = await _connection(s, name="Модель пространства", model="m-space")
        space.model_connection_id = space_model.id
        await settings_store.set_value(s, meetings.SPACE_KEY, str(space.id))

        assert (await meetings.routing_model(s)).model == "m-space"


async def test_a_deleted_or_switched_off_choice_falls_back_instead_of_stopping(db):
    """Указатель на пропавшее подключение не должен тихо выключать разбор встреч —
    ровно то же правило, что у указателя на удалённое пространство."""
    async with db() as s:
        space = await mk_space(s)
        await _account(s)
        await _connection(s, name="Общая по умолчанию", model="m-global", is_default=True)
        space_model = await _connection(s, name="Модель пространства", model="m-space")
        space.model_connection_id = space_model.id
        await settings_store.set_value(s, meetings.SPACE_KEY, str(space.id))

        off = await _connection(s, name="Выключенная", model="m-off", enabled=False)
        await settings_store.set_value(s, meetings.MODEL_KEY, str(off.id))
        assert (await meetings.routing_model(s)).model == "m-space"

        await settings_store.set_value(s, meetings.MODEL_KEY, str(uuid.uuid4()))
        assert (await meetings.routing_model(s)).model == "m-space"


async def test_the_screen_reports_the_model_the_handler_will_actually_use(db):
    """Текст блокера и обработчик обязаны смотреть на один и тот же резолв: иначе экран
    скажет «всё готово», а разбор упадёт на отсутствующем ключе."""
    async with db() as s:
        await mk_space(s)
        await _account(s)
        await _project(s, "Поставки")
        keyless = ModelConnection(name="Без ключа", provider="openai", kind="openai",
                                  base_url=None, api_key_enc=None, model="m-nokey",
                                  enabled=True)
        s.add(keyless)
        await s.flush()
        await settings_store.set_value(s, meetings.MODEL_KEY, str(keyless.id))

        assert await meetings.routing_blocker(s) == "Не настроена модель для разбора встреч."


# --- title (§6.1) ---


async def test_model_topic_replaces_the_first_line_of_the_transcript(
    db, tmp_path, monkeypatch
):
    """Пока разбора нет, заголовок — первая фраза записи; она почти никогда не тема.
    Разобрав встречу целиком, модель называет её по существу, и это название побеждает."""
    async with db() as s:
        await mk_space(s)
        await _account(s)
        await _project(s, "Поставки")
        m = await _meeting(s, tmp_path)
        assert m.title is None
        _stub(monkeypatch, title="Планёрка по поставкам: сроки и смета")
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert m.title == "Планёрка по поставкам: сроки и смета"


async def test_a_long_topic_is_cut_to_the_column_width(db, tmp_path, monkeypatch):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        await _project(s, "Поставки")
        m = await _meeting(s, tmp_path)
        _stub(monkeypatch, title="  " + "тема " * 40)
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert len(m.title) == meetings._TITLE_MAX_CHARS
        assert m.title.startswith("тема")


async def test_a_silent_model_leaves_the_transcript_title_alone(db, tmp_path, monkeypatch):
    """Пустой ответ модели не должен стирать то немногое, что уже известно о встрече."""
    async with db() as s:
        await mk_space(s)
        await _account(s)
        await _project(s, "Поставки")
        m = await _meeting(s, tmp_path)
        _stub(monkeypatch, title="   ")
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert m.title == "Планёрка по поставкам, обсуждаем сроки."


# --- extracted items (§6.1, §8.5) ---


async def test_items_are_stored_even_when_no_project_matched(db, tmp_path, monkeypatch):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        await _project(s, "Поставки")
        m = await _meeting(s, tmp_path)
        _stub(monkeypatch, items=[
            MeetingItem(type=ActionType.DELEGATED_TASK, title="Смету подготовить",
                        counterparty="Иванов", due_date="2026-08-21", at="00:23:10"),
            MeetingItem(type=ActionType.DECISION, title="Поставщика меняем на второго",
                        at="00:31:40", due_date="2026-08-30"),
        ], matches=[])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()

        items = {i.type: i for i in await _items(s, m.id)}
        assert set(items) == {ActionType.DELEGATED_TASK, ActionType.DECISION}
        assert items[ActionType.DELEGATED_TASK].due_date.isoformat() == "2026-08-21"
        assert items[ActionType.DELEGATED_TASK].reason.startswith("[00:23:10]")
        # A decision has no lifecycle: any deadline the model invents is dropped, so it
        # can never surface in «Сегодня».
        assert items[ActionType.DECISION].due_date is None
        today = [i.title for i in await views.list_view(s, "today")]
        assert "Поставщика меняем на второго" not in today


async def test_reextraction_recreates_auto_items_but_never_the_ones_i_ruled_on(
    db, tmp_path, monkeypatch
):
    done = MeetingItem(type=ActionType.MY_COMMITMENT, title="Позвонить поставщику")
    dropped = MeetingItem(type=ActionType.TASK, title="Курс вырос на 3%")
    kept = MeetingItem(type=ActionType.DELEGATED_TASK, title="Смету подготовить",
                       counterparty="Иванов")
    async with db() as s:
        await mk_space(s)
        await _account(s)
        await _project(s, "Поставки")
        m = await _meeting(s, tmp_path)
        _stub(monkeypatch, items=[done, dropped, kept])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()

        by_title = {i.title: i for i in await _items(s, m.id)}
        # complete() marks resolved_by='user' but NOT user_edited; dismiss() sets both.
        await views.complete(s, by_title["Позвонить поставщику"].id)
        await views.dismiss(s, by_title["Курс вырос на 3%"].id)
        auto_id = by_title["Смету подготовить"].id
        await s.commit()

        # Same meeting re-delivered: the model reports the same three plus a new one.
        new = MeetingItem(type=ActionType.DECISION, title="Поставщика меняем")
        _stub(monkeypatch, items=[done, dropped, kept, new])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()

        rows = {i.title: i for i in await _items(s, m.id)}
        assert len(rows) == 4  # nothing duplicated
        assert rows["Позвонить поставщику"].status == ActionStatus.DONE
        assert rows["Курс вырос на 3%"].status == ActionStatus.DISMISSED
        assert rows["Смету подготовить"].id != auto_id  # untouched auto row: recreated
        assert rows["Поставщика меняем"].status == ActionStatus.OPEN


async def test_two_tasks_for_the_same_person_both_survive(db, tmp_path, monkeypatch):
    # The thread key (source, type, counterparty) would have lost the second one — a
    # meeting is a batch of distinct items, not one evolving obligation.
    async with db() as s:
        await mk_space(s)
        await _account(s)
        await _project(s, "Поставки")
        m = await _meeting(s, tmp_path)
        _stub(monkeypatch, items=[
            MeetingItem(type=ActionType.DELEGATED_TASK, title="Смета", counterparty="Иванов"),
            MeetingItem(type=ActionType.DELEGATED_TASK, title="Бронь зала",
                        counterparty="Иванов"),
        ])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert {i.title for i in await _items(s, m.id)} == {"Смета", "Бронь зала"}


# --- not configured: visible, not silent ---


@pytest.mark.parametrize("what", ["projects", "other_space"])
async def test_routing_leaves_a_visible_note_when_it_cannot_run(
    db, tmp_path, monkeypatch, what
):
    """The «no mailbox chosen» case is gone with spaces (a meeting always has a space);
    its place is taken by «the only opted-in project is in ANOTHER space»."""
    async with db() as s:
        await mk_space(s)
        await _account(s)
        if what == "projects":
            await _project(s, "Почтовый", include=False)  # opted out
        else:
            other = await mk_space(s, 2)
            p = await _project(s, "Поставки")
            p.space_id = other.id  # opted in, but not where the meeting lands
            await s.flush()
        m = await _meeting(s, tmp_path)
        calls = _stub(monkeypatch)
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()

        assert calls["route"] == 0  # no model call when the answer can't be used
        assert m.route_note
        assert await _members(s, m.id) == {}


async def test_a_missing_transcript_is_reported_not_swallowed(db, tmp_path, monkeypatch):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        await _project(s, "Поставки")
        m = await _meeting(s, tmp_path)
        (tmp_path / "ASROutput/2026-08/rec.txt").unlink()
        calls = _stub(monkeypatch)
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert calls["route"] == 0
        assert "Расшифровка не найдена" in m.route_note


async def test_an_empty_transcript_is_called_empty_not_missing(db, tmp_path, monkeypatch):
    """Диктофон оставил файл в 0 байт: «не найдена» отправляло искать пропавший архив,
    хотя файл на месте."""
    async with db() as s:
        await mk_space(s)
        await _account(s)
        await _project(s, "Поставки")
        m = await _meeting(s, tmp_path, text=" \n")
        calls = _stub(monkeypatch)
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert calls["route"] == 0
        assert m.routed_at is None and "Расшифровка пустая" in m.route_note


async def test_routing_space_setting_is_honoured(db, tmp_path, monkeypatch):
    """Rewritten for spaces: the chosen space gets the meeting, its projects and its
    tasks; the default space sees nothing of it."""
    async with db() as s:
        await mk_space(s)
        home = await mk_space(s, 2)
        await _account(s, "Работа", "work@y.ru")
        await settings_store.set_value(s, meetings.SPACE_KEY, str(home.id))
        p = await _project(s, "Поставки")
        p.space_id = home.id
        await s.flush()
        m = await _meeting(s, tmp_path)
        assert m.space_id == home.id
        calls = _stub(monkeypatch, matches=[
            MeetingMatch(project="Поставки", member=True, confidence=0.9),
        ])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert calls["route"] == 2  # договорённости и раскладка — отдельными вызовами
        assert len(await _members(s, m.id)) == 1
        assert m.routed_at is not None and m.routed_at <= datetime.now(UTC)


async def test_meeting_tasks_follow_the_meetings_space_in_the_dossier(db, tmp_path):
    """Per-space isolation in the person dossier (rewritten for spaces): a meeting task
    is in the dossier of its space and in no other space's."""
    async with db() as s:
        await mk_space(s)
        home = await mk_space(s, 2)
        await _account(s, "Работа", "work@y.ru")
        await settings_store.set_value(s, meetings.SPACE_KEY, str(SPACE_ID))
        m = await _meeting(s, tmp_path)
        s.add(ActionItem(space_id=m.space_id, type=ActionType.MY_COMMITMENT.value,
                         title="Смета к пятнице", counterparty="Иванов",
                         source_meeting_id=m.id, status="open"))
        await s.flush()

        mine = await views.person_dossier(s, "Иванов", SPACE_ID)
        assert [i.title for i in mine["items"]] == ["Смета к пятнице"]
        # The other space never sees it, however the dossier was opened.
        other = await views.person_dossier(s, "Иванов", home.id)
        assert other["items"] == []


async def test_a_meeting_that_returned_early_is_not_retried_by_the_queue(
    db, tmp_path, monkeypatch
):
    """Событие, вернувшееся раньше срока, для очереди «выполнено» — само оно не
    повторится. Именно это и чинит кнопка «Разобрать» на экране «Встречи»."""
    async with db() as s:
        await mk_space(s)
        await _account(s)
        m = await _meeting(s, tmp_path)
        _stub(monkeypatch)

        # Ни один проект не помечен «учитывать встречи»: разбор невозможен.
        assert await meetings.routing_blocker(s) is not None
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await _drain(s)
        assert m.routed_at is None and "не помечен" in m.route_note
        assert [x.ext_id for x in await meetings.unprocessed_meetings(s)] == ["meet0001"]

        # Пока причина не устранена — в очередь не ставим, а объясняем.
        assert "не помечен" in await meetings.routing_blocker(s)
        await _project(s, "Поставки")
        assert await meetings.routing_blocker(s) is None

        assert await meetings.requeue_unprocessed(s) == 1
        assert m.route_note is None, "старая причина снята — её решают заново"
        # Повторное нажатие не платит второй раз: событие уже в очереди.
        assert await meetings.unprocessed_meetings(s) == []
        assert await meetings.requeue_unprocessed(s) == 0


def test_result_drops_items_a_meeting_cannot_produce():
    """Cheap models emit mail-only types and empty titles; losing one item must not cost
    the whole extraction."""
    r = MeetingRoutingResult.model_validate({
        "summary": "s",
        "items": [
            {"type": "NEED_MY_REPLY", "title": "на встрече не бывает", "confidence": 0.9},
            {"type": "DECISION", "title": "  ", "confidence": 0.9},
            {"type": "DECISION", "title": "решили менять поставщика", "confidence": 0.9},
        ],
        "matches": [{"project": "П", "member": True, "confidence": 0.9}],
    })
    assert [i.title for i in r.items] == ["решили менять поставщика"]
    assert r.matches[0].project == "П"


def test_a_stored_error_is_explained_in_words():
    """402 разбирается раньше общего «недоступен»: кончившиеся деньги — не сетевой сбой,
    и ждать, что само пройдёт, не надо."""
    from email_reader.ai.errors import explain

    assert explain('APIStatusError("Error code: 402 - {\'error\': \'Insufficient balance\'}")') \
        == "у провайдера модели кончились средства"
    assert explain("AuthenticationError: Error code: 401") == "провайдер не принял ключ"
    assert explain("RateLimitError: Error code: 429") == "провайдер ограничил частоту запросов"
    assert explain("APIConnectionError: connection reset") == "провайдер был недоступен"
    # Незнакомое не прячем — по тексту хотя бы можно искать.
    assert explain("ValueError: что-то своё") == "провайдер ответил: ValueError: что-то своё"
    assert explain(None) is None and explain("   ") is None


async def _second_meeting(s, tmp_path, text: str = TRANSCRIPT) -> Meeting:
    rel = "ASROutput/2026-08/rec2.txt"
    (tmp_path / rel).write_text(text, encoding="utf-8")
    return await meetings.ingest_payload(s, {
        "meeting_id": "meet0002", "started_at": "2026-08-17T10:00:00+03:00",
        "asr_root": str(tmp_path), "transcript_path": rel, "source": "test",
    })


def _event(meeting, status, error=None):
    from email_reader.enums import EventType
    from email_reader.models import Event

    payload = {"meeting_id": str(meeting.id)}
    if error:
        payload["last_error"] = error
    return Event(type=str(EventType.MEETING_READY), status=status, payload=payload)


NO_MONEY = "APIStatusError(\"Error code: 402 - {'error': 'Insufficient balance'}\")"


async def test_the_screen_says_why_the_last_routing_attempt_failed(db, tmp_path):
    """Человек жал «Разобрать», девять встреч падали на 402, а на экране не было ничего:
    последней в пачке шла встреча с пустой расшифровкой, её событие «выполнено», и
    экран смотрел только на него. Причина берётся по каждой неразобранной встрече."""
    from email_reader.enums import EventStatus

    async with db() as s:
        await mk_space(s)
        broke = await _meeting(s, tmp_path)
        empty = await _second_meeting(s, tmp_path, text="")
        await _drain(s)
        stuck = await meetings.unprocessed_meetings(s)
        assert await meetings.last_routing_failure(s, stuck) is None  # не пробовали — молчим

        s.add(_event(broke, EventStatus.FAILED, NO_MONEY))
        await s.flush()
        s.add(_event(empty, EventStatus.DONE))  # позже в той же пачке
        await s.commit()
        stuck = await meetings.unprocessed_meetings(s)
        assert await meetings.last_routing_failure(s, stuck) == \
            "у провайдера модели кончились средства"

        # Починили и разобрали: прошлая ошибка с экрана уходит сама.
        broke.routed_at = datetime.now(UTC)
        await s.commit()
        stuck = await meetings.unprocessed_meetings(s)
        assert [m.id for m in stuck] == [empty.id]
        assert await meetings.last_routing_failure(s, stuck) is None


async def test_a_meeting_waiting_for_its_model_is_shown_and_not_queued_twice(db, tmp_path):
    """Отложенный до ответа модели разбор вернётся сам. Второе событие рядом с ним — это
    второй платный разбор той же записи, поэтому кнопка его не ставит, а экран говорит,
    чего встреча ждёт."""
    from email_reader.enums import EventStatus

    async with db() as s:
        await mk_space(s)
        await _project(s, "Поставки")
        m = await _meeting(s, tmp_path)
        await _drain(s)
        s.add(_event(m, EventStatus.STALLED, NO_MONEY))
        await s.commit()

        assert await meetings.unprocessed_meetings(s) == []
        assert await meetings.requeue_unprocessed(s) == 0
        assert await meetings.parked_meetings(s) == (1, "у провайдера модели кончились средства")

        m.routed_at = datetime.now(UTC)
        await s.commit()
        assert await meetings.parked_meetings(s) == (0, None)


async def test_an_empty_answer_keeps_the_meetings_tasks_and_threads(db, tmp_path, monkeypatch):
    """Рассуждающая модель съела лимит, шлюз отдал вызов инструмента без аргументов.
    Раньше это читалось как «задач нет» и стирало задачи встречи и её Нити."""
    async with db() as s:
        await mk_space(s)
        await _account(s)
        crabs = await _project(s, "Крабы")
        m = await _meeting(s, tmp_path, TRANSCRIPT)
        _stub(monkeypatch, matches=[
            MeetingMatch(project="Крабы", member=True, confidence=0.9, reason="про крабов"),
        ], items=[MeetingItem(type=ActionType.TASK, title="Прислать смету")])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await _drain(s)
        _stub(monkeypatch, summary=None)
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        titles = [i.title for i in await _items(s, m.id)]
        members = await _members(s, m.id)
        await s.refresh(m)
    assert titles == ["Прислать смету"] and crabs.id in members
    assert "пустой разбор" in m.route_note


async def test_an_empty_routing_call_keeps_the_meetings_threads(db, tmp_path, monkeypatch):
    """Договорённости пришли, а раскладка по Нитям — пустой: это сбой второго вызова,
    привязки встречи остаются, а задачи обновляются."""
    async with db() as s:
        await mk_space(s)
        await _account(s)
        crabs = await _project(s, "Крабы")
        m = await _meeting(s, tmp_path, TRANSCRIPT)
        _stub(monkeypatch, matches=[
            MeetingMatch(project="Крабы", member=True, confidence=0.9, reason="про крабов"),
        ])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await _drain(s)
        _stub(monkeypatch, items=[MeetingItem(type=ActionType.TASK, title="Прислать смету")])
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        titles = [i.title for i in await _items(s, m.id)]
        members = await _members(s, m.id)
    assert titles == ["Прислать смету"] and crabs.id in members
