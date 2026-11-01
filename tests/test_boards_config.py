"""Настраиваемые доски: доска — отбор по колонкам, а не промпт (docs/ux-tasks.md §17.5).

Две породы записей. У пресета предикат и секции живут в Python, в записи меняются имя,
иконка, порядок и видимость. У своей доски (`filter`) типы, условие и сортировка читаются
из записи. Пресет нельзя удалить: его ключ знают бот, MCP и сохранённые ссылки.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from email_reader import boards as boards_svc
from email_reader import spaces as spaces_svc
from email_reader import views
from email_reader.enums import ActionStatus, ActionType
from email_reader.models import ActionItem
from tests.conftest import SPACE_ID, mk_space


def _card(type_, *, title="t", due=None, candidate=False, days_old=0, delegate_to=None):
    stamp = datetime.now(UTC) - timedelta(days=days_old)
    return ActionItem(
        space_id=SPACE_ID, type=str(type_), title=title, status=ActionStatus.OPEN,
        due_date=due, close_candidate=candidate, created_at=stamp, updated_at=stamp,
        delegate_to=delegate_to,
    )


async def test_a_new_space_gets_the_preset_boards(db):
    async with db() as s:
        space = await spaces_svc.create_space(s, "Новое")
        specs = await boards_svc.list_specs(s, space.id)
        assert [b.key for b in specs] == list(boards_svc.PRESET_KINDS)
        assert all(b.builtin for b in specs)
        # Ключ пресета равен виду: /view/today и cookie группировки остаются рабочими.
        assert [b.kind for b in specs] == [b.key for b in specs]


async def test_seeding_twice_adds_nothing(db):
    async with db() as s:
        space = await spaces_svc.create_space(s, "Новое")
        assert await boards_svc.seed(s, space.id) == 0


async def test_hiding_a_board_keeps_it_reachable_by_url(db):
    """Скрытая доска уходит из меню, но не из приложения: сохранённая ссылка не должна
    приводить в никуда."""
    async with db() as s:
        await mk_space(s)
        await boards_svc.update_spec(s, SPACE_ID, "triage", hidden=True)
        visible = await boards_svc.list_specs(s, SPACE_ID, visible_only=True)
        assert "triage" not in [b.key for b in visible]
        assert await boards_svc.get_spec(s, SPACE_ID, "triage") is not None
        # И ведём на первую видимую, а не на жёстко зашитую «Сегодня».
        assert await boards_svc.landing_key(s, SPACE_ID) == "today"
        await boards_svc.update_spec(s, SPACE_ID, "today", hidden=True)
        assert await boards_svc.landing_key(s, SPACE_ID) == "needreply"


async def test_a_preset_cannot_be_deleted_only_hidden(db):
    async with db() as s:
        await mk_space(s)
        assert await boards_svc.delete_spec(s, SPACE_ID, "waiting") is False
        assert await boards_svc.get_spec(s, SPACE_ID, "waiting") is not None


async def test_a_board_without_types_is_refused(db):
    """Доска без типов показывала бы всё и ничего не значила."""
    async with db() as s:
        await mk_space(s)
        assert await boards_svc.create_filter(s, SPACE_ID, name="Пустая", types=[]) is None
        assert await boards_svc.create_filter(
            s, SPACE_ID, name="Мусор", types=["НЕ_ТИП"]
        ) is None


async def test_custom_boards_are_capped(db):
    async with db() as s:
        await mk_space(s)
        made = 0
        for n in range(boards_svc.MAX_CUSTOM + 3):
            spec = await boards_svc.create_filter(
                s, SPACE_ID, name=f"Своя {n}", types=[str(ActionType.TASK)]
            )
            made += spec is not None
        assert made == boards_svc.MAX_CUSTOM


async def test_a_custom_board_shows_the_types_it_was_given(db):
    async with db() as s:
        await mk_space(s)
        s.add_all([
            _card(ActionType.TASK, title="моя задача"),
            _card(ActionType.NEED_MY_REPLY, title="чужой вопрос"),
        ])
        await s.flush()
        spec = await boards_svc.create_filter(
            s, SPACE_ID, name="Только задачи", types=[str(ActionType.TASK)]
        )
        board = await views.board(s, spec.key, SPACE_ID)
        assert [r.item.title for r in board.rows] == ["моя задача"]


async def test_a_custom_board_honours_its_condition(db):
    async with db() as s:
        await mk_space(s)
        s.add_all([
            _card(ActionType.TASK, title="со сроком", due=date(2020, 1, 1)),
            _card(ActionType.TASK, title="без срока"),
        ])
        await s.flush()
        spec = await boards_svc.create_filter(
            s, SPACE_ID, name="Без сроков", types=[str(ActionType.TASK)], condition="no_due"
        )
        board = await views.board(s, spec.key, SPACE_ID)
        assert [r.item.title for r in board.rows] == ["без срока"]


async def test_a_custom_board_honours_its_sort(db):
    """Сортировка своей доски — выбранная пользователем, а не питонов порядок пресетов:
    иначе настройка ничего бы не делала."""
    async with db() as s:
        await mk_space(s)
        s.add_all([
            _card(ActionType.TASK, title="старая", days_old=10),
            _card(ActionType.TASK, title="новая", days_old=1),
        ])
        await s.flush()
        newest = await boards_svc.create_filter(
            s, SPACE_ID, name="Новые сверху", types=[str(ActionType.TASK)],
            sort="created_desc",
        )
        oldest = await boards_svc.create_filter(
            s, SPACE_ID, name="Старые сверху", types=[str(ActionType.TASK)],
            sort="created_asc",
        )
        assert [r.item.title for r in (await views.board(s, newest.key, SPACE_ID)).rows] == [
            "новая", "старая",
        ]
        assert [r.item.title for r in (await views.board(s, oldest.key, SPACE_ID)).rows] == [
            "старая", "новая",
        ]


async def test_the_close_offer_is_the_last_section_on_a_custom_board_too(db):
    async with db() as s:
        await mk_space(s)
        s.add_all([
            _card(ActionType.TASK, title="живая"),
            _card(ActionType.TASK, title="залежалась", candidate=True),
        ])
        await s.flush()
        spec = await boards_svc.create_filter(
            s, SPACE_ID, name="Задачи", types=[str(ActionType.TASK)], sort="created_asc"
        )
        board = await views.board(s, spec.key, SPACE_ID)
        assert [r.item.title for r in board.rows] == ["живая", "залежалась"]
        assert board.sections == [("Предлагаю закрыть", 1)]


async def test_editing_types_of_a_preset_is_ignored(db):
    """У пресета предикат в Python — принять типы значило бы соврать, что они действуют."""
    async with db() as s:
        await mk_space(s)
        await boards_svc.update_spec(
            s, SPACE_ID, "waiting", types=[str(ActionType.TASK)], condition="no_due"
        )
        spec = await boards_svc.get_spec(s, SPACE_ID, "waiting")
        assert spec.types == [str(ActionType.WAITING_FOR_REPLY)]
        assert (spec.condition or "") == ""


async def test_cloning_a_preset_inherits_its_types(db):
    async with db() as s:
        await mk_space(s)
        clone = await boards_svc.clone_as_filter(s, SPACE_ID, "delegated")
        assert clone is not None
        assert clone.builtin is False
        assert clone.kind == boards_svc.FILTER
        assert set(clone.types) == {
            str(ActionType.TO_DELEGATE), str(ActionType.DELEGATED_TASK)
        }
        # Пресет на месте: его можно спрятать, но копия его не заменяет.
        assert await boards_svc.get_spec(s, SPACE_ID, "delegated") is not None


async def test_counts_cover_custom_boards(db):
    async with db() as s:
        await mk_space(s)
        s.add(_card(ActionType.TASK, title="моя"))
        await s.flush()
        spec = await boards_svc.create_filter(
            s, SPACE_ID, name="Мои", types=[str(ActionType.TASK)]
        )
        cnt = await views.counts(s, SPACE_ID)
        assert cnt[spec.key] == 1
        # Архив не считаем: число закрытого растёт бесконечно.
        assert "done" not in cnt


async def test_reorder_moves_boards(db):
    async with db() as s:
        await mk_space(s)
        keys = [b.key for b in await boards_svc.list_specs(s, SPACE_ID)]
        await boards_svc.reorder(s, SPACE_ID, list(reversed(keys)))
        assert [b.key for b in await boards_svc.list_specs(s, SPACE_ID)] == list(
            reversed(keys)
        )


async def test_boards_go_away_with_their_space(db):
    async with db() as s:
        space = await spaces_svc.create_space(s, "На удаление")
        assert await boards_svc.list_specs(s, space.id)
        await s.delete(space)
        await s.flush()
        assert await boards_svc.list_specs(s, space.id) == []


async def test_mcp_lists_a_custom_board_too(db):
    """Снаружи (MCP) доска адресуется своим ключом в своём пространстве — иначе собранная
    пользователем доска остаётся ассистенту невидимой."""
    from email_reader.mcp import tools as mcp_tools

    async with db() as s:
        space = await mk_space(s)
        s.add(_card(ActionType.TASK, title="моя"))
        await s.flush()
        spec = await boards_svc.create_filter(
            s, space.id, name="Мои задачи по-своему", types=[str(ActionType.TASK)]
        )
        out = await mcp_tools.list_cards(s, space=space.name, board=spec.key)
        assert out["board_name"] == "Мои задачи по-своему"
        assert [c["title"] for c in out["cards"]] == ["моя"]


async def test_mcp_names_the_boards_of_that_space_when_the_key_is_wrong(db):
    from email_reader.mcp import tools as mcp_tools

    async with db() as s:
        space = await mk_space(s)
        try:
            await mcp_tools.list_cards(s, space=space.name, board="нет-такой")
        except Exception as exc:  # ToolError
            assert "today" in str(exc) and space.name in str(exc)
        else:
            raise AssertionError("ожидалась ошибка про неизвестную доску")


async def test_drop_target_keeps_the_historical_choice_for_presets(db):
    """Перенос строки мышью на доску: у пресета на два типа исторически выбран один."""
    async with db() as s:
        await mk_space(s)
        by_key = {b.key: b for b in await boards_svc.list_specs(s, SPACE_ID)}
        assert boards_svc.drop_type(by_key["delegated"]) == str(ActionType.DELEGATED_TASK)
        assert boards_svc.drop_type(by_key["commitments"]) == str(ActionType.MY_COMMITMENT)
        assert boards_svc.drop_type(by_key["snoozed"]) == "snoozed"
        # «Сегодня» и «Разобрать» целью переноса не были и не стали.
        assert boards_svc.drop_type(by_key["today"]) == ""
        one = await boards_svc.create_filter(
            s, SPACE_ID, name="Одна", types=[str(ActionType.TASK)]
        )
        two = await boards_svc.create_filter(
            s, SPACE_ID, name="Две", types=[str(ActionType.TASK), str(ActionType.INFO)]
        )
        assert boards_svc.drop_type(one) == str(ActionType.TASK)
        # Доска на несколько типов целью не становится: выбор был бы произвольным.
        assert boards_svc.drop_type(two) == ""


async def test_the_no_owner_condition_finds_handoffs_nobody_took(db):
    """«Договорились действовать, но ответственного нет» — условие доски, а не флаг от
    планировщика: снимать тут нечего, чинится назначением (docs/feature-card-advice.md §1.8)."""
    async with db() as s:
        await mk_space(s)
        s.add_all([
            _card(ActionType.TO_DELEGATE, title="некому", days_old=9),
            _card(ActionType.TO_DELEGATE, title="есть кому", delegate_to="Борис", days_old=9),
            _card(ActionType.TASK, title="моя задача", days_old=9),
        ])
        await s.flush()
        spec = await boards_svc.create_filter(
            s, SPACE_ID, name="Без ответственного",
            types=[str(ActionType.TO_DELEGATE), str(ActionType.TASK)], condition="no_owner",
        )
        board = await views.board(s, spec.key, SPACE_ID)
        assert [r.item.title for r in board.rows] == ["некому"]
        assert "no_owner" in boards_svc.CONDITIONS


# --- Имена пресетов ---------------------------------------------------------------------
# Доска называется действием, которого она просит. Переименование пресета в коде не должно
# переименовывать доску, которую пользователь назвал сам, — этим занимается миграция,
# трогающая только записи со старым значением по умолчанию.
async def test_the_reply_board_is_named_by_its_action(db):
    async with db() as s:
        space = await spaces_svc.create_space(s, "Новое")
        spec = await boards_svc.get_spec(s, space.id, "needreply")
    assert spec.name == "Ответить"


async def test_a_board_the_user_renamed_keeps_its_name(db):
    async with db() as s:
        space = await spaces_svc.create_space(s, "Новое")
        spec = await boards_svc.get_spec(s, space.id, "needreply")
        await boards_svc.update_spec(s, space.id, spec.key, name="Отписаться")
        again = await boards_svc.get_spec(s, space.id, "needreply")
    assert again.name == "Отписаться"
