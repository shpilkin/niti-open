"""The project folder: who owns which files, and what a re-classification does to them.

The folder has three writers (mail export, meeting export, the user), so these tests are
mostly about boundaries: the machine writes six places and nowhere else, generated files
say they are generated, and nothing the classifier decides ever deletes a file.
"""

from __future__ import annotations

import pytest

from email_reader import meetings, project_folder, settings_store, views
from email_reader import projects as projects_svc
from email_reader.ai.schemas import MeetingItem, MeetingMatch
from email_reader.enums import ActionType
from email_reader.models import Project
from tests.conftest import SPACE_ID, mk_space
from tests.test_meeting_routing import _account, _meeting, _project, _stub


def _read(path):
    return path.read_text(encoding="utf-8")


# --- where the folder is ---


def test_explicit_folder_wins_over_the_shared_root(tmp_path):
    explicit = Project(
        space_id=SPACE_ID, name="Крабы", folder_path=str(tmp_path / "здесь"), prompt="")
    shared = Project(space_id=SPACE_ID, name="Смета", folder_path=None, prompt="")
    root = str(tmp_path / "Проекты")
    assert project_folder.folder_for(explicit, root) == tmp_path / "здесь"
    assert project_folder.folder_for(shared, root) == tmp_path / "Проекты" / "Смета"
    # Neither set: the project simply has nowhere to be exported yet.
    assert project_folder.folder_for(shared, None) is None
    # A name that would escape the root or break the filesystem is flattened.
    nasty = Project(space_id=SPACE_ID, name="../../etc", folder_path=None, prompt="")
    assert project_folder.folder_for(nasty, root).parent == tmp_path / "Проекты"


def test_the_shared_root_is_checked_when_it_is_SET_not_at_the_first_export(tmp_path):
    """A bad root breaks every project without its own folder, so it is rejected at the
    moment it is set — not N exports later, far from the action that caused it."""
    ok = tmp_path / "Проекты"
    assert project_folder.validate_root(str(ok)) == ok
    # ~ is a path a human types; it has to end up absolute.
    assert project_folder.validate_root("~/Документы/Проекты").is_absolute()

    with pytest.raises(project_folder.ExportBlocked):
        project_folder.validate_root("Документы/Проекты")      # относительный

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    # Per-project _ensure_writable would never catch this: <repo>/<name> has no .git of
    # its own, so every project would quietly land inside the repository.
    with pytest.raises(project_folder.ExportBlocked):
        project_folder.validate_root(str(repo))

    afile = tmp_path / "заметка.txt"
    afile.write_text("x", encoding="utf-8")
    with pytest.raises(project_folder.ExportBlocked):
        project_folder.validate_root(str(afile))


def test_export_refuses_to_write_into_a_source_repository(tmp_path):
    base = tmp_path / "repo"
    (base / ".git").mkdir(parents=True)
    with pytest.raises(project_folder.ExportBlocked):
        project_folder._ensure_writable(base)


async def test_shared_root_is_used_when_a_project_has_no_folder(db, tmp_path):
    async with db() as s:
        await mk_space(s)
        await settings_store.set_value(
            s, project_folder.PROJECTS_ROOT_KEY, str(tmp_path / "Проекты")
        )
        p = await projects_svc.create_project(
            s, space_id=SPACE_ID, name="Крабы", prompt="про крабов"
        )
        base = await project_folder.project_dir(s, p, create=True)
    assert base == tmp_path / "Проекты" / "Крабы"
    assert base.is_dir()


# --- what a routed meeting leaves on disk ---


async def _route(s, tmp_path, monkeypatch, *, matches, items):
    m = await _meeting(s, tmp_path)
    _stub(monkeypatch, matches=matches, items=items)
    await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
    await s.commit()
    return m


ITEMS = [
    MeetingItem(type=ActionType.DELEGATED_TASK, title="Подготовить смету",
                counterparty="Иванов", due_date="2026-08-21", at="00:23:10"),
    MeetingItem(type=ActionType.DECISION, title="Поставщика меняем на второго",
                at="00:31:40"),
    MeetingItem(type=ActionType.INFO, title="Курс вырос на 3%"),
]


async def test_one_meeting_two_projects_two_different_notes(db, tmp_path, monkeypatch):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        a = await _project(s, "Поставки")
        b = await _project(s, "Смета")
        a.folder_path = str(tmp_path / "Поставки")
        b.folder_path = str(tmp_path / "Смета")
        await _route(
            s, tmp_path, monkeypatch,
            matches=[
                MeetingMatch(project="Поставки", member=True, confidence=0.9,
                             reason="сроки", digest="Сдвинули поставку на неделю."),
                MeetingMatch(project="Смета", member=True, confidence=0.85,
                             reason="смета", digest="Смету готовит Иванов."),
            ],
            items=ITEMS,
        )

    notes_a = list((tmp_path / "Поставки" / "Встречи").glob("*.md"))
    notes_b = list((tmp_path / "Смета" / "Встречи").glob("*.md"))
    assert len(notes_a) == 1 and len(notes_b) == 1
    text_a, text_b = _read(notes_a[0]), _read(notes_b[0])
    # Same meeting, same front matter — different text per project.
    assert "Сдвинули поставку на неделю." in text_a
    assert "Смету готовит Иванов." in text_b
    assert "meeting_id: meet0001" in text_a and "meeting_id: meet0001" in text_b
    assert "projects: [Поставки, Смета]" in text_a
    assert "source: auto" in text_a and "confidence: 0.90" in text_a
    # Items are listed with their timecode, and the transcript is linked, not copied.
    assert "- [ ] Подготовить смету — Иванов (срок 2026-08-21) `00:23:10`" in text_a
    assert "Полный транскрипт" in text_a
    assert "[00:00:04] Спикер 1" not in text_a


async def test_the_note_keeps_the_whole_meeting_and_the_project_part_apart(
    db, tmp_path, monkeypatch
):
    """Два разных блока, а не один.

    Общее описание нужно, чтобы понять, откуда фрагмент; фрагмент — единственное, ради
    чего заметка лежит в ЭТОЙ папке. Раньше summary подставлялось вместо digest, когда
    его не было, и «общее» с «про проект» сливались в один текст."""
    async with db() as s:
        await mk_space(s)
        await _account(s)
        a = await _project(s, "Поставки")
        b = await _project(s, "Смета")
        a.folder_path = str(tmp_path / "Поставки")
        b.folder_path = str(tmp_path / "Смета")
        await _route(
            s, tmp_path, monkeypatch,
            matches=[
                MeetingMatch(project="Поставки", member=True, confidence=0.9,
                             digest="**О чём фрагмент.** Сроки поставки краба."),
                MeetingMatch(project="Смета", member=True, confidence=0.85,
                             digest="**О чём фрагмент.** Смета и бюджет."),
            ],
            items=ITEMS,
        )

    text = _read(next((tmp_path / "Поставки" / "Встречи").glob("*.md")))
    assert "## О чём была встреча целиком" in text
    assert "о чём говорили" in text                      # общее саммари встречи
    assert "## Что из этого относится к проекту «Поставки»" in text
    assert "Сроки поставки краба." in text
    # Чужой фрагмент в эту папку не попадает — это и есть «остальное не используется».
    assert "Смета и бюджет." not in text
    # Расшифровка не копируется: в папке только ссылка на оригинал в архиве.
    assert "transcript: " in text and "Полный транскрипт" in text
    assert "[00:00:04] Спикер 1" not in text


async def test_a_missing_digest_says_so_instead_of_borrowing_the_general_summary(
    db, tmp_path, monkeypatch
):
    """Подстановка общего саммари вместо фрагмента выглядела как разбор по проекту,
    хотя разбора не было. Молчание честнее подделки."""
    async with db() as s:
        await mk_space(s)
        await _account(s)
        a = await _project(s, "Поставки")
        a.folder_path = str(tmp_path / "Поставки")
        await _route(
            s, tmp_path, monkeypatch,
            matches=[MeetingMatch(project="Поставки", member=True, confidence=0.9)],
            items=[],
        )

    text = _read(next((tmp_path / "Поставки" / "Встречи").glob("*.md")))
    assert "## О чём была встреча целиком" in text and "о чём говорили" in text
    assert "не выделила фрагмент" in text


async def test_summaries_are_a_projection_of_the_database(db, tmp_path, monkeypatch):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        p = await _project(s, "Поставки")
        p.folder_path = str(tmp_path / "Поставки")
        await _route(
            s, tmp_path, monkeypatch,
            matches=[MeetingMatch(project="Поставки", member=True, confidence=0.9)],
            items=ITEMS,
        )
        base = tmp_path / "Поставки"
        assert "Подготовить смету" in _read(base / "Задачи.md")
        assert "Поставщика меняем на второго" in _read(base / "Решения.md")
        # Справок со встреч больше нет (27.09.2026): наблюдение живёт в сводке встречи.
        assert "Курс вырос на 3%" not in _read(base / "Информация.md")
        # Every generated file says so — the folder is shared with the user's own notes.
        for name in ("README.md", "Задачи.md", "Решения.md", "Информация.md"):
            assert "формируется автоматически" in _read(base / name)

        # Closing the task drops it from Задачи.md on the next write; decisions stay
        # (they have no lifecycle).
        done = next(i for i in await project_folder.items_for_project(s, p.id)
                    if i.type == ActionType.DELEGATED_TASK)
        await views.complete(s, done.id)
        await s.commit()
        await project_folder.write_summaries(s, p, base)
        assert "Подготовить смету" not in _read(base / "Задачи.md")
        assert "Поставщика меняем на второго" in _read(base / "Решения.md")


async def test_user_folders_are_never_touched(db, tmp_path, monkeypatch):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        p = await _project(s, "Поставки")
        p.folder_path = str(tmp_path / "Поставки")
        notes = tmp_path / "Поставки" / "Заметки"
        notes.mkdir(parents=True)
        mine = notes / "мои мысли.md"
        mine.write_text("моё, руками", encoding="utf-8")
        await _route(
            s, tmp_path, monkeypatch,
            matches=[MeetingMatch(project="Поставки", member=True, confidence=0.9)],
            items=ITEMS,
        )
    assert _read(mine) == "моё, руками"
    assert [p.name for p in notes.iterdir()] == ["мои мысли.md"]


async def test_dropped_project_keeps_its_note_but_marks_it_stale(db, tmp_path, monkeypatch):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        p = await _project(s, "Поставки")
        p.folder_path = str(tmp_path / "Поставки")
        m = await _route(
            s, tmp_path, monkeypatch,
            matches=[MeetingMatch(project="Поставки", member=True, confidence=0.9,
                                  digest="про поставки")],
            items=ITEMS,
        )
        note = next((tmp_path / "Поставки" / "Встречи").glob("*.md"))

        # Re-routing with the opposite verdict: membership goes, the file stays.
        _stub(monkeypatch, matches=[
            MeetingMatch(project="Поставки", member=False, confidence=0.1),
        ], items=ITEMS)
        await meetings.process_meeting_ready(s, {"meeting_id": str(m.id)})
        await s.commit()
        assert await projects_svc.projects_for_meeting(s, m.id) == []

    assert note.exists()
    text = _read(note)
    assert "stale: true" in text
    assert "больше не относится к проекту" in text


async def test_a_git_folder_blocks_that_project_only(db, tmp_path, monkeypatch):
    async with db() as s:
        await mk_space(s)
        await _account(s)
        repo = await _project(s, "Код")
        good = await _project(s, "Поставки")
        repo.folder_path = str(tmp_path / "Код")
        good.folder_path = str(tmp_path / "Поставки")
        (tmp_path / "Код" / ".git").mkdir(parents=True)
        (tmp_path / "Код" / "README.md").write_text("репозиторий", encoding="utf-8")
        await _route(
            s, tmp_path, monkeypatch,
            matches=[
                MeetingMatch(project="Код", member=True, confidence=0.9),
                MeetingMatch(project="Поставки", member=True, confidence=0.9),
            ],
            items=ITEMS,
        )
    # The repo's own README survives; the other project is exported normally.
    assert _read(tmp_path / "Код" / "README.md") == "репозиторий"
    assert not (tmp_path / "Код" / "Встречи").exists()
    assert list((tmp_path / "Поставки" / "Встречи").glob("*.md"))
