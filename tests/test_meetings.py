"""Meeting intake (v0): the folder handover from the recorder app.

Covers what the contract promises and what a producer can realistically get wrong: a
re-delivered meeting must land on the same row (ids come from the audio content, not the
file name), a half-written or incomplete payload must not be silently lost, and the
transcript link must survive the archive moving.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from email_reader import meetings, settings_store
from email_reader import spaces as spaces_svc
from email_reader.enums import EventType
from email_reader.models import AgentDecision, Event, Meeting
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space

STARTED = "2026-08-16T14:30:05+03:00"


def _payload(root, **over) -> dict:
    p = {
        "meeting_id": "8f3a1c4e2b7d9a01",
        "started_at": STARTED,
        "duration_sec": 2820,
        "asr_root": str(root),
        "transcript_path": "ASROutput/2026-08/rec.txt",
        "srt_path": "ASROutput/2026-08/rec.srt",
        "audio_path": "Processed/2026-08/rec.m4a",
        "source": "recorder/1.0",
    }
    p.update(over)
    return p


def _write_transcript(root, text: str, rel: str = "ASROutput/2026-08/rec.txt"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def inbox(db, monkeypatch, tmp_path):
    """scan_inbox() with its own sessions pointed at the test DB (like the worker's)."""
    @asynccontextmanager
    async def scope():
        async with db() as s:
            await mk_space(s)
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    monkeypatch.setattr(meetings, "session_scope", scope)
    return meetings.inbox_dir()


# --- ingest ---


async def test_ingest_creates_meeting_and_queues_event(db, tmp_path):
    async with db() as s:
        await mk_space(s)
        m = await meetings.ingest_payload(s, _payload(tmp_path))
        await s.commit()
        assert m is not None
        assert m.ext_id == "8f3a1c4e2b7d9a01"
        # 14:30:05+03:00 is 11:30:05 UTC — stored normalized, not as wall-clock.
        assert m.started_at == datetime(2026, 8, 16, 11, 30, 5, tzinfo=UTC)
        assert m.duration_sec == 2820
        assert m.transcript_path == "ASROutput/2026-08/rec.txt"
        assert m.source == "recorder/1.0"
        assert await settings_store.get_value(s, meetings.ASR_ROOT_KEY) == str(tmp_path)
        events = (await s.execute(select(Event))).scalars().all()
        assert [e.type for e in events] == [EventType.MEETING_READY]
        assert events[0].payload == {"meeting_id": str(m.id)}


async def test_ingest_is_idempotent_on_ext_id(db, tmp_path):
    async with db() as s:
        await mk_space(s)
        first = await meetings.ingest_payload(s, _payload(tmp_path))
        first_id = first.id
        # Same meeting re-delivered after the archive was reorganized.
        again = await meetings.ingest_payload(
            s, _payload(tmp_path, transcript_path="ASROutput/moved/rec.txt")
        )
        await s.commit()
        assert again.id == first_id
        assert len((await s.execute(select(Meeting))).scalars().all()) == 1
        assert again.transcript_path == "ASROutput/moved/rec.txt"
        # Re-delivery re-queues: the transcript may have been redone.
        assert len((await s.execute(select(Event))).scalars().all()) == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"meeting_id": ""},
        {"started_at": "не дата"},
        {"started_at": ""},
        {"transcript_path": ""},
    ],
)
async def test_ingest_rejects_incomplete_payload(db, tmp_path, bad):
    async with db() as s:
        await mk_space(s)
        assert await meetings.ingest_payload(s, _payload(tmp_path, **bad)) is None
        assert (await s.execute(select(Meeting))).scalars().all() == []


async def test_naive_started_at_is_read_as_local_time(db, tmp_path):
    async with db() as s:
        await mk_space(s)
        m = await meetings.ingest_payload(
            s, _payload(tmp_path, started_at="2026-08-16T14:30:05")
        )
        await s.commit()
    expected = datetime(2026, 8, 16, 14, 30, 5).astimezone(UTC)
    assert m.started_at == expected


# --- inbox sweep ---


async def test_scan_ingests_then_removes_the_json(db, inbox, tmp_path):
    (inbox / "a.json").write_text(json.dumps(_payload(tmp_path)), encoding="utf-8")
    assert await meetings.scan_inbox() == {"ingested": 1, "rejected": 0}
    assert not (inbox / "a.json").exists()
    async with db() as s:
        await mk_space(s)
        assert len((await s.execute(select(Meeting))).scalars().all()) == 1


async def test_scan_leaves_a_fresh_broken_json_but_quarantines_a_stale_one(inbox):
    """A half-written file must survive one sweep (the producer may still be writing);
    one that is still broken minutes later is moved aside, never deleted."""
    fresh = inbox / "fresh.json"
    fresh.write_text('{"meeting_id": "aa', encoding="utf-8")
    stale = inbox / "stale.json"
    stale.write_text('{"meeting_id": "bb', encoding="utf-8")
    old = (datetime.now(UTC) - timedelta(minutes=30)).timestamp()
    os.utime(stale, (old, old))

    assert await meetings.scan_inbox() == {"ingested": 0, "rejected": 1}
    assert fresh.exists()
    assert not stale.exists()
    assert (inbox / "rejected" / "stale.json").exists()


async def test_scan_quarantines_an_incomplete_payload(db, inbox, tmp_path):
    (inbox / "b.json").write_text(
        json.dumps(_payload(tmp_path, meeting_id="")), encoding="utf-8"
    )
    assert await meetings.scan_inbox() == {"ingested": 0, "rejected": 1}
    assert (inbox / "rejected" / "b.json").exists()
    async with db() as s:
        await mk_space(s)
        assert (await s.execute(select(Meeting))).scalars().all() == []


# --- path resolution ---


async def test_resolve_follows_the_archive_root(db, tmp_path):
    target = _write_transcript(tmp_path, "текст")
    async with db() as s:
        await mk_space(s)
        await settings_store.set_value(s, meetings.ASR_ROOT_KEY, str(tmp_path))
        assert await meetings.resolve(s, "ASROutput/2026-08/rec.txt") == target
        # Root changed (archive moved) and the file isn't there -> no stale path returned.
        await settings_store.set_value(s, meetings.ASR_ROOT_KEY, str(tmp_path / "other"))
        assert await meetings.resolve(s, "ASROutput/2026-08/rec.txt") is None


async def test_resolve_refuses_to_escape_the_archive_root(db, tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("нельзя", encoding="utf-8")
    root = tmp_path / "asr"
    root.mkdir()
    async with db() as s:
        await mk_space(s)
        await settings_store.set_value(s, meetings.ASR_ROOT_KEY, str(root))
        assert await meetings.resolve(s, "../secret.txt") is None


# --- MeetingReady handler ---


async def test_handler_titles_the_meeting_from_the_transcript(db, tmp_path):
    _write_transcript(tmp_path, "[00:00:04] Спикер 1: Планёрка по крабам, сроки поставки.\n")
    async with db() as s:
        await mk_space(s)
        m = await meetings.ingest_payload(s, _payload(tmp_path))
        await s.commit()
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert m.title == "Планёрка по крабам, сроки поставки."


async def test_handler_survives_a_missing_transcript(db, tmp_path):
    async with db() as s:
        await mk_space(s)
        m = await meetings.ingest_payload(s, _payload(tmp_path))  # no file written
        await s.commit()
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        assert m.title is None


async def test_handler_ignores_an_unknown_meeting(db):
    async with db() as s:
        await mk_space(s)
        await meetings.process_meeting_ready(s, {"meeting_id": str(uuid.uuid4())})


def test_transcript_title_keeps_a_sentence_with_a_colon():
    # «Обсудили сроки» is the start of the sentence, not a speaker label — keep it whole.
    assert meetings.transcript_title(
        "1\n00:00:01\nОбсудили сроки: они сдвигаются на неделю"
    ) == "Обсудили сроки: они сдвигаются на неделю"


def test_transcript_title_strips_a_named_speaker():
    assert meetings.transcript_title("Иван Петров: добрый день") == "добрый день"
    assert meetings.transcript_title("") is None


# --- routing mailbox + board scoping ---


async def _account(s, name: str, login: str, enabled: bool = True):
    from email_reader.models import Account

    a = Account(space_id=SPACE_ID, name=name, provider="yandex", host="h", port=993, login=login,
                password_enc="x", folders=["INBOX"], enabled=enabled)
    s.add(a)
    await s.flush()
    return a


async def test_routing_space_defaults_to_the_first_space(db):
    """Rewritten for spaces: meetings always have somewhere to land — the chosen space,
    else the default one (there is always a space, docs/spaces.md §3)."""
    async with db() as s:
        assert await meetings.routing_space_id(s) is not None  # created on demand
        await mk_space(s)
        await mk_space(s, 2)
        assert await meetings.routing_space_id(s) == (await spaces_svc.list_spaces(s))[0].id


async def test_routing_space_setting_wins_but_a_dead_id_falls_back(db):
    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        await settings_store.set_value(s, meetings.SPACE_KEY, str(SPACE_ID_2))
        assert await meetings.routing_space_id(s) == SPACE_ID_2
        # Setting points at a space that was deleted -> the default, not silence.
        await settings_store.set_value(s, meetings.SPACE_KEY, str(uuid.uuid4()))
        assert await meetings.routing_space_id(s) == SPACE_ID


async def test_meeting_items_are_on_the_boards_of_the_meetings_space(db, tmp_path):
    """Rewritten for spaces: a new meeting lands in the configured space, and its tasks
    are on that space's boards — and on no other space's."""
    from email_reader import views
    from email_reader.enums import ActionStatus, ActionType
    from email_reader.models import ActionItem

    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        await settings_store.set_value(s, meetings.SPACE_KEY, str(SPACE_ID))
        m = await meetings.ingest_payload(s, _payload(tmp_path))
        assert m.space_id == SPACE_ID
        s.add(ActionItem(
            space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="ответить по смете",
                         status=ActionStatus.OPEN, source_meeting_id=m.id))
        await s.commit()

        titles = [i.title for i in await views.list_view(s, "needreply", space_id=SPACE_ID)]
        assert titles == ["ответить по смете"]
        assert await views.count_view(s, "needreply", space_id=SPACE_ID) == 1
        # The other mailbox must not see it.
        assert await views.list_view(s, "needreply", space_id=SPACE_ID_2) == []
        # No filter -> visible as well.
        assert len(await views.list_view(s, "needreply")) == 1


# --- Where in the transcript a task said at the meeting points to ---

TRANSCRIPT = (
    "[00:00:04] Спикер 1: Планёрка по крабам.\n"
    "[00:08:00] Спикер 2: Про воркфлоу для бэклогов.\n"
    "[00:12:30] Спикер 1: Смету пришлю в пятницу.\n"
)


def test_timecode_span_takes_the_turn_that_was_running():
    """The model quotes a moment it heard, and that exact second is usually not a stamp
    in the file — the turn it fell into is what the reader needs."""
    span = meetings.timecode_span(TRANSCRIPT, "[00:08:23]")
    assert TRANSCRIPT[span[0] : span[1]] == "[00:08:00] Спикер 2: Про воркфлоу для бэклогов.\n"


def test_timecode_span_on_an_exact_stamp():
    span = meetings.timecode_span(TRANSCRIPT, "[00:12:30]")
    assert TRANSCRIPT[span[0] : span[1]] == "[00:12:30] Спикер 1: Смету пришлю в пятницу.\n"


def test_timecode_span_before_the_first_stamp_points_nowhere():
    assert meetings.timecode_span(TRANSCRIPT, "[00:00:01]") is None


def test_timecode_span_without_a_timecode():
    assert meetings.timecode_span(TRANSCRIPT, "просто текст") is None
    assert meetings.timecode_span("", "[00:08:23]") is None


def test_quoted_timecode():
    assert meetings.quoted_timecode("[00:08:23] сказано на встрече") == "[00:08:23]"
    assert meetings.quoted_timecode("без времени") is None
    assert meetings.quoted_timecode(None) is None


# --- Delete (soft, undoable) and move between spaces ---


async def _meeting_with_items(s, tmp_path):
    from email_reader.enums import ActionType
    from email_reader.models import ActionItem

    m = await meetings.ingest_payload(s, _payload(tmp_path))
    mine = ActionItem(space_id=SPACE_ID, type=ActionType.TASK, title="моё", status="open",
                      source_meeting_id=m.id, user_edited=True)
    auto = ActionItem(space_id=SPACE_ID, type=ActionType.TASK, title="авто", status="open",
                      source_meeting_id=m.id)
    done = ActionItem(space_id=SPACE_ID, type=ActionType.TASK, title="сделано", status="done",
                      resolved_by="user", source_meeting_id=m.id)
    s.add_all([mine, auto, done])
    await s.flush()
    return m, mine, auto, done


async def test_delete_hides_the_meeting_and_closes_its_machine_items(db, tmp_path):
    from sqlalchemy import delete as sa_delete

    async with db() as s:
        await mk_space(s)
        m, mine, auto, done = await _meeting_with_items(s, tmp_path)
        await s.execute(sa_delete(Event))  # nothing queued: the meeting counts as unprocessed
        await s.commit()
        assert [x.id for x in await meetings.unprocessed_meetings(s)] == [m.id]

        assert await meetings.delete_meeting(s, m.id) is m
        await s.commit()
        assert await meetings.list_meetings(s, space_id=SPACE_ID) == []
        assert await meetings.count_meetings(s, space_id=SPACE_ID) == 0
        assert await meetings.unprocessed_meetings(s) == []
        assert (auto.status, auto.resolved_by) == ("done", meetings.MEETING_DELETED)
        assert mine.status == "open"  # the user's own — never touched
        assert (done.status, done.resolved_by) == ("done", "user")
        # A re-delivered recording does not bring it back, nor queue a routing pass.
        again = await meetings.ingest_payload(s, _payload(tmp_path, duration_sec=99))
        assert again is m and m.deleted_at is not None and m.duration_sec == 2820
        assert (await s.execute(select(Event))).scalars().all() == []

        # Undo: the meeting is back, and exactly the items the deletion closed.
        assert await meetings.restore_meeting(s, m.id) is m
        await s.commit()
        assert [x.id for x in await meetings.list_meetings(s, space_id=SPACE_ID)] == [m.id]
        assert (auto.status, auto.resolved_by) == ("open", None)
        assert (done.status, done.resolved_by) == ("done", "user")


async def test_move_takes_the_items_and_drops_the_old_spaces_projects(db, tmp_path):
    from email_reader import projects as projects_svc
    from email_reader.models import ProjectMeeting

    async with db() as s:
        await mk_space(s)
        other = await mk_space(s, 2)
        m, mine, auto, done = await _meeting_with_items(s, tmp_path)
        work = await projects_svc.create_project(s, name="Смета", prompt="", space_id=SPACE_ID)
        home = await projects_svc.create_project(s, name="Дача", prompt="", space_id=other.id)
        s.add(ProjectMeeting(project_id=work.id, meeting_id=m.id, source="manual"))
        m.suggestions = [{"project_id": str(work.id), "confidence": 0.7, "reason": "смета"}]
        await s.commit()

        assert await meetings.set_space(s, m.id, other.id) is True
        await s.commit()
        assert m.space_id == other.id
        assert {i.space_id for i in (mine, auto, done)} == {other.id}
        assert [p.id for p in await projects_svc.projects_for_meeting(s, m.id)] == []
        assert m.suggestions == []
        assert [x.id for x in await meetings.list_meetings(s, space_id=other.id)] == [m.id]
        assert await meetings.list_meetings(s, space_id=SPACE_ID) == []
        # A project of the new space, filed by hand, survives a move back and forth.
        s.add(ProjectMeeting(project_id=home.id, meeting_id=m.id, source="manual"))
        await s.commit()
        assert await meetings.set_space(s, m.id, other.id) is True  # no-op
        assert [p.id for p in await projects_svc.projects_for_meeting(s, m.id)] == [home.id]
        assert await meetings.set_space(s, m.id, uuid.uuid4()) is False  # unknown space


async def test_handler_leaves_a_deleted_meeting_alone(db, tmp_path):
    """The scan queues MEETING_READY, the user deletes the meeting a moment later: routing
    it would title it and put live items into its space."""
    _write_transcript(tmp_path, "[00:00:04] Спикер 1: Планёрка по крабам, сроки поставки.\n")
    async with db() as s:
        await mk_space(s)
        m = await meetings.ingest_payload(s, _payload(tmp_path))
        await meetings.delete_meeting(s, m.id)
        await s.commit()
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert (m.title, m.route_note, m.routed_at) == (None, None, None)


async def test_a_deleted_meeting_leaves_its_project_page_and_folder_summaries(db, tmp_path):
    from email_reader import project_folder
    from email_reader import projects as projects_svc
    from email_reader.models import ProjectMeeting

    async with db() as s:
        await mk_space(s)
        m, mine, auto, done = await _meeting_with_items(s, tmp_path)
        p = await projects_svc.create_project(s, name="Смета", prompt="", space_id=SPACE_ID)
        s.add(ProjectMeeting(project_id=p.id, meeting_id=m.id, source="manual"))
        await s.commit()
        assert [x.id for x in await projects_svc.meetings_for_project(s, p.id)] == [m.id]
        assert len(await project_folder.items_for_project(s, p.id)) == 3
        await meetings.delete_meeting(s, m.id)
        await s.commit()
        assert await projects_svc.meetings_for_project(s, p.id) == []
        # The folder summaries are a projection of the live meetings: nothing of it stays.
        assert await project_folder.items_for_project(s, p.id) == []
        await meetings.restore_meeting(s, m.id)
        await s.commit()
        assert [x.id for x in await projects_svc.meetings_for_project(s, p.id)] == [m.id]
        assert len(await project_folder.items_for_project(s, p.id)) == 3


async def test_a_deleted_meeting_does_not_keep_its_space_alive(db):
    """Delete the only meeting of a space, then the space: the blocker must not count a
    meeting nothing shows and nothing can move; its row goes with the space."""
    async with db() as s:
        await mk_space(s)
        other = await mk_space(s, 2)
        m = Meeting(ext_id="deadbeef00", space_id=other.id, started_at=datetime.now(UTC))
        s.add(m)
        await s.commit()
        assert "встречи" in await spaces_svc.delete_blocker(s, other.id)
        await meetings.delete_meeting(s, m.id)
        await s.commit()
        assert await spaces_svc.delete_blocker(s, other.id) is None
        assert await spaces_svc.delete_space(s, other.id) is None
        await s.commit()
        s.expire_all()
        assert await s.get(Meeting, m.id) is None
        assert [sp.id for sp in await spaces_svc.list_spaces(s)] == [SPACE_ID]


async def test_routing_prompt_carries_memory_and_the_journal_says_what(db, tmp_path, monkeypatch):
    """The meeting prompt gets the same block the mail extractor gets: the owner's
    profile, the participants' notes, the facts that touch the title."""
    from email_reader import profiles
    from email_reader import projects as projects_svc
    from email_reader.ai import extractor
    from email_reader.ai.schemas import MeetingRoutingResult
    from email_reader.memory import people as people_svc
    from email_reader.model_connections import ResolvedModel

    seen: dict = {}

    async def fake_fold(transcript, **kw):  # noqa: ANN001
        return transcript, 0

    async def fake_route(fold, projects, today, hints=None, **kw):  # noqa: ANN001
        seen.update(kw)
        return MeetingRoutingResult(title="Лесково: соглашение"), extractor.Usage()

    async def fake_model(session, space_id=None):  # noqa: ANN001
        return ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")

    monkeypatch.setattr(extractor, "fold_transcript", fake_fold)
    monkeypatch.setattr(extractor, "route_meeting", fake_route)
    monkeypatch.setattr(meetings, "routing_model", fake_model)

    _write_transcript(tmp_path, "[00:00:04] Спикер 1: Обсуждаем редакцию соглашения с Лесково.\n")
    async with db() as s:
        await mk_space(s)
        await projects_svc.create_project(
            s, name="Лесково", prompt="партнёрство", space_id=SPACE_ID, include_meetings=True,
        )
        await profiles.set_profile(s, SPACE_ID, "Роль: PM.")
        await profiles.remember_line(s, SPACE_ID, "по Лесково согласую с юристами")
        olya = await people_svc.ensure_person_by_name(s, "Оля Иванова", source="meeting")
        await people_svc.set_notes(s, olya, "ведёт аналитику")
        m = await meetings.ingest_payload(s, _payload(tmp_path))
        m.participants = ["Оля Иванова"]
        await s.commit()
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        journal = (await s.execute(
            select(AgentDecision).where(AgentDecision.kind == "meeting_route")
        )).scalar_one()

    prompt = seen["system_prompt"]
    assert "О ВЛАДЕЛЬЦЕ И КОМАНДАХ" in prompt and "Роль: PM." in prompt
    assert "ПАМЯТЬ (справочные данные о людях, проектах" in prompt
    assert "КОНТРАГЕНТ: Оля Иванова\n  Заметки владельца: ведёт аналитику" in prompt
    assert "по Лесково согласую с юристами" in prompt
    memory = journal.payload["memory"]
    assert memory["people"] == ["Оля Иванова"] and len(memory["facts"]) == 1


def test_a_structured_summary_splits_into_sections_and_an_old_one_stays_whole():
    text = ("О чём: разбор прототипа, я и двое разработчиков.\n"
            "Обсудили:\n- формат чата сработал\n- нет уведомлений\n"
            "**Договорились:**\n- Оля — выгрузка из Jira — к пятнице\n"
            "Открытые вопросы:\n- кто ведёт поддержку")
    blocks = meetings.summary_blocks(text)
    assert [(b.head, b.text, b.points) for b in blocks] == [
        ("О чём", ["разбор прототипа, я и двое разработчиков."], []),
        ("Обсудили", [], ["формат чата сработал", "нет уведомлений"]),
        ("Договорились", [], ["Оля — выгрузка из Jira — к пятнице"]),
        ("Открытые вопросы", [], ["кто ведёт поддержку"]),
    ]
    assert meetings.summary_gist(text) == "разбор прототипа, я и двое разработчиков."
    old = "Обсудили сроки поставки и смету.\nДоговорились о пятнице."
    assert [(b.head, b.text) for b in meetings.summary_blocks(old)] == [
        (None, ["Обсудили сроки поставки и смету.", "Договорились о пятнице."])]
    assert meetings.summary_gist(old) == "Обсудили сроки поставки и смету. Договорились о пятнице."


def test_a_meeting_no_longer_files_observations_as_cards():
    from email_reader.ai.schemas import MeetingRoutingResult

    got = MeetingRoutingResult.model_validate({"items": [
        {"type": "INFO", "title": "формат чата сработал"},
        {"type": "DELEGATED_TASK", "title": "Выгрузка из Jira", "counterparty": "Оля"},
    ]})
    assert [i.title for i in got.items] == ["Выгрузка из Jira"]


def test_the_summary_is_built_from_the_sections_the_model_fills():
    from email_reader.ai.schemas import MeetingRoutingResult

    got = MeetingRoutingResult.model_validate({
        "about": "разбор прототипа", "discussed": ["чат сработал", " "],
        "agreed": ["Оля — выгрузка — пятница"], "open_questions": [], "items": [],
        "matches": [],
    })
    assert got.summary == ("О чём: разбор прототипа\nОбсудили:\n- чат сработал\n"
                           "Договорились:\n- Оля — выгрузка — пятница")
    assert meetings.summary_gist(got.summary) == "разбор прототипа"
    # Старый ответ одним текстом — как есть.
    assert MeetingRoutingResult(summary="текст").summary == "текст"
