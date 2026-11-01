"""Playwright smoke tests for the web UI (the layer pytest+TestClient can't cover):
sidebar render, search, opening a message, compose cancel, context menu, source filter,
and per-account collapse. Run:  pytest -m e2e tests/e2e
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_home_and_sidebar(page: Page):
    page.goto("/view/today")
    expect(page.locator("text=ИИ-рекомендации")).to_be_visible()
    expect(page.locator(".nav-cap:has-text('Почта')")).to_be_visible()
    expect(page.locator(".nav-cap:has-text('Почта') .proj-add")).to_be_visible()  # "+"


def test_search_finds_email(page: Page):
    page.goto("/view/today")
    page.fill(".search-box", "договор")
    expect(page.locator("#list")).to_contain_text("Договор аренды")


def test_open_email_shows_reading_pane(page: Page):
    page.goto("/mail/inbox")
    page.locator(".mrow").first.click()
    expect(page.locator(".read-subject")).to_be_visible()
    # Действия письма — иконками (как в почтовых клиентах); подпись живёт в title.
    expect(page.locator(".read-actions button[title='Ответить']")).to_be_visible()
    expect(page.locator(".read-actions button[title='Переслать']")).to_be_visible()


def test_compose_cancel_closes(page: Page):
    page.goto("/mail/inbox")
    page.locator(".main-head button[title='Написать письмо']").click()
    expect(page.locator(".compose-form")).to_be_visible()
    page.locator(".wz-actions button:has-text('Отмена')").click()
    expect(page.locator(".compose-form")).to_have_count(0)
    expect(page.locator(".pane-empty")).to_be_visible()


def test_reply_cancel_returns_to_message(page: Page):
    # Regression: a reply/forward composer's Cancel used an hx-get attribute on a
    # button the #pane swap then deleted; with /email/{id}'s countsChanged trigger
    # htmx threw "htmx-internal-data" and the composer stayed open. Must now close
    # and return to the message — with no page errors.
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/mail/inbox")
    page.locator(".mrow").first.click()
    # Let the reading pane fully settle (htmx binds the buttons + its load-triggered
    # sub-requests for html/projects finish) before clicking, otherwise the click can
    # land before htmx has wired the button.
    expect(page.locator(".read-actions")).to_be_visible()
    page.wait_for_load_state("networkidle")
    page.locator(".read-actions button[title='Ответить']").click()
    expect(page.locator(".compose-form")).to_be_visible()
    page.locator(".wz-actions button:has-text('Отмена')").click()
    expect(page.locator(".compose-form")).to_have_count(0)
    expect(page.locator(".read-actions")).to_be_visible()  # back on the message
    assert errors == [], f"JS errors on cancel: {errors}"


def test_context_menu_on_mail_row(page: Page):
    page.goto("/mail/inbox")
    page.locator(".mrow").first.click(button="right")
    menu = page.locator("#ctx-menu")
    expect(menu).to_be_visible()
    expect(menu).to_contain_text("Ответить")
    expect(menu).to_contain_text("Переслать")
    expect(menu).to_contain_text("Создать обязательство")


def test_space_switcher(page: Page):
    """Rewritten for spaces (docs/spaces.md §4): the window is one space. The seeded
    NEED_MY_REPLY item belongs to «Работа»; «Дом» never shows it. A single-mailbox space
    has no mailbox select in the toolbar — there is nothing to filter by."""
    page.goto("/view/needreply")
    expect(page.locator("#space-switch select")).to_have_value(re.compile(".+"))
    expect(page.locator("#board-tools")).to_be_visible()
    expect(page.locator("#board-filter")).to_have_count(0)
    expect(page.locator("#list")).to_contain_text("ответить по договору")
    page.select_option("#space-switch select", label="Дом")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#list")).not_to_contain_text("ответить по договору")
    expect(page.locator(".acct-cap:has-text('Дом')")).to_be_visible()  # the space's mailbox
    page.select_option("#space-switch select", label="Работа")  # leave it as found
    page.wait_for_load_state("networkidle")
    expect(page.locator("#list")).to_contain_text("ответить по договору")


def test_meetings_list_and_transcript_pane(page: Page):
    page.goto("/view/today")
    page.locator(".nav-item:has-text('Встречи')").click()
    expect(page.locator(".mail-toolbar")).to_contain_text("Проверить папку")
    row = page.locator(".mrow:has-text('Планёрка по крабам')")
    expect(row).to_be_visible()
    expect(row).to_contain_text("Поставки")  # the project it was filed into
    row.click()
    # The transcript is read from disk through the archive-root setting, not stored in DB.
    expect(page.locator("#pane")).to_contain_text("Расшифровка")
    expect(page.locator("#pane")).to_contain_text("Смету пришлю в пятницу")
    expect(page.locator("#pane")).to_contain_text("Подготовить смету")  # extracted item


def test_confirming_a_suggested_project_files_the_meeting(page: Page, click_and_settle):
    page.goto("/meetings")
    click_and_settle(page.locator(".mrow:has-text('Планёрка по крабам')"))
    pane = page.locator("#pane")
    expect(pane.locator(".sugg-row")).to_contain_text("Смета")
    pane.locator(".sugg-row button:has-text('Подтвердить')").click()
    # Confirmed -> it becomes a membership chip and stops being a suggestion.
    expect(pane.locator(".proj-chip:has-text('Смета')")).to_be_visible()
    expect(pane.locator(".sugg-row")).to_have_count(0)


def test_unrouted_filter_hides_a_routed_meeting(page: Page):
    """Замысел прежний — разложенная встреча из фильтра «Без нити» пропадает.
    Раньше это проверялось пустым состоянием списка; теперь в посеве есть ещё и
    неразобранная запись без проекта, поэтому проверяем прямо: одна видна, другая нет."""
    page.goto("/meetings")
    page.wait_for_load_state("networkidle")
    page.locator(".src-chip:has-text('Без нити')").click()
    expect(page.locator("#list")).not_to_contain_text("Планёрка по крабам")
    expect(page.locator("#list")).to_contain_text("Не разобранная планёрка")
    page.locator(".src-chip:has-text('Все')").click()
    expect(page.locator("#list")).to_contain_text("Планёрка по крабам")


def test_account_group_collapse(page: Page):
    page.goto("/view/today")
    cap = page.locator(".acct-cap:has-text('Работа')")
    expect(cap.locator(".acct-arrow")).to_have_text("▾")
    cap.click()
    expect(cap.locator(".acct-arrow")).to_have_text("▸")  # collapsed


def test_the_ui_works_with_no_internet(page: Page):
    """Настольное приложение не должно зависеть от сети, чтобы работали кнопки.

    Когда htmx тянулся с unpkg и не грузился, доска рисовалась как обычно, кнопка
    нажималась, ошибок не было — и нажатие просто не доходило до сервера. Тест
    режет ВСЁ внешнее: если что-то снова уедет на CDN, он покраснеет."""
    page.route("**://*/**", lambda route: (
        route.continue_() if "127.0.0.1" in route.request.url or "localhost" in route.request.url
        else route.abort()
    ))
    page.goto("/view/needreply?account=")
    page.wait_for_load_state("networkidle")
    card = page.locator(".trow:has-text('ответить по договору')")
    card.hover()                                   # .actions{display:none} до :hover
    card.locator(".act.ok").click()
    expect(page.locator("#list")).not_to_contain_text("ответить по договору")


def test_meeting_task_on_the_board_opens_its_meeting(page: Page, click_and_settle):
    """A task extracted from a recording must be traceable back to what was said.

    A mail task shows its thread in the pane; a meeting task has no thread, so the only
    way back to the transcript is the source tag — and it has to actually go there."""
    page.goto("/view/today?account=")          # explicit: ignore the remembered filter
    page.wait_for_load_state("networkidle")
    card = page.locator(".trow:has-text('Подготовить смету')")
    expect(card.locator(".trow-src")).to_have_attribute("title", re.compile("Планёрка по крабам"))
    click_and_settle(card.locator(".trow-title"))
    pane = page.locator("#pane")
    expect(pane.locator("a.src-tag")).to_contain_text("Планёрка по крабам")
    pane.locator("a.src-tag").click()
    # The meeting pane renders in the board's #pane too, not only on /meetings.
    expect(pane).to_contain_text("Расшифровка")
    expect(pane).to_contain_text("Смету пришлю в пятницу")


def test_meeting_task_is_on_its_spaces_board(page: Page):
    """Rewritten for spaces: a task spoken at a meeting is on the boards of the space the
    meeting landed in («Работа») and on no other space's."""
    page.goto("/view/today?account=")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#list")).to_contain_text("Подготовить смету")
    page.select_option("#space-switch select", label="Дом")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#list")).not_to_contain_text("Подготовить смету")
    page.select_option("#space-switch select", label="Работа")  # leave it as found
    page.wait_for_load_state("networkidle")


def test_person_dossier_from_a_meeting_task_keeps_one_space(page: Page, click_and_settle):
    """The dossier behind a meeting task is scoped to the meeting's space («Работа»).

    Both halves matter: the meeting task itself has to survive that scoping, and mail
    the same person sent to the OTHER space's mailbox must not leak in."""
    page.goto("/view/today?account=")
    page.wait_for_load_state("networkidle")
    click_and_settle(page.locator(".trow:has-text('Подготовить смету') .trow-title"))
    pane = page.locator("#pane")
    expect(pane).to_contain_text("Подготовить смету")
    pane.locator("a:has-text('Иванов')").click()
    expect(pane).to_contain_text("Подготовить смету")
    expect(pane).not_to_contain_text("частная переписка")


def test_project_page_lists_its_meetings(page: Page):
    page.goto("/projects")
    page.locator(".acc-row:has-text('Поставки') a:has-text('Открыть')").click()
    expect(page.locator("#list")).to_contain_text("Встречи")
    expect(page.locator("#list .mrow:has-text('Планёрка по крабам')")).to_be_visible()


def test_projects_root_is_settable_and_visible_where_projects_live(page: Page):
    """Настройка была: поле лежало на вкладке «Встречи», хотя она про проекты, и найти
    её было негде. Теперь она в «Общих» с нативным выбором папки, а страница «Проекты»
    показывает действующий корень."""
    page.goto("/settings/section/general")
    field = page.locator("#projects-root")
    expect(field).to_be_visible()
    # Тот же нативный выбор папки, что и у папки отдельного проекта.
    expect(page.locator("button[onclick*=\'projects-root\']")).to_be_visible()

    # Относительный путь ломал бы каждый проект без своей папки — отлуп на сохранении.
    field.fill("Документы/Проекты")
    page.locator("form:has(#projects-root) button:has-text('Сохранить')").click()
    expect(page.locator("#projects-root-status")).to_contain_text("абсолютный путь")

    field.fill("/tmp/niti-e2e-projects")
    page.locator("form:has(#projects-root) button:has-text('Сохранить')").click()
    expect(page.locator("#projects-root-status")).to_contain_text("Сохранено")

    page.goto("/projects")
    expect(page.locator("#list")).to_contain_text("/tmp/niti-e2e-projects")


def test_prompt_groups_collapse_per_space(page: Page):
    """Промптов четыре на пространство, пространств несколько — вкладка без сворачивания
    нечитаема. Механизм общий с сайдбаром, но ключ свой: свёрнутая здесь группа не должна
    сворачивать группу ящика с тем же именем в сайдбаре."""
    page.goto("/settings/section/prompts")
    cap = page.locator(".prompt-acct[data-acct-toggle]:has-text('Работа')")
    gid = cap.get_attribute("data-acct-toggle")
    group = page.locator(f"[data-acct-group='{gid}']")
    expect(cap.locator(".acct-arrow")).to_have_text("▾")
    expect(group).to_be_visible()

    cap.click()
    expect(cap.locator(".acct-arrow")).to_have_text("▸")
    expect(group).not_to_be_visible()

    # Сайдбар тем же именем ящика не тронут.
    expect(page.locator(".acct-cap:has-text('Работа') .acct-arrow")).to_have_text("▾")

    cap.click()
    expect(group).to_be_visible()


def test_unprocessed_meetings_offer_a_button_or_the_reason(page: Page):
    """Событие, вернувшееся раньше срока, для очереди «выполнено» — встреча иначе
    осталась бы «не разобрана» навсегда. Экран обязан показывать и счёт, и путь дальше:
    кнопку, когда разбор возможен, и причину со ссылкой, когда нет."""
    page.goto("/meetings")
    banner = page.locator(".banner.warn")
    expect(banner).to_contain_text("Не разобрано встреч: 1")
    banner.locator("button:has-text('Разобрать')").click()
    # Ставится в очередь; работу делает воркер, экран лишь честно про это говорит.
    expect(page.locator(".banner.ok")).to_contain_text("Поставлено в очередь: 1")
    # Счётчик пересобран из БД: повторно платить за ту же встречу не за что.
    expect(page.locator(".banner.warn")).to_have_count(0)


def test_transcript_downloads_as_a_txt_file(page: Page):
    """Кнопка отдаёт ФАЙЛ из архива, а не то, что показано на экране (там текст обрезан),
    и с внятным именем — по нему должно быть видно, что за запись."""
    page.goto("/meetings")
    page.locator(".mrow:has-text('Планёрка по крабам')").click()
    link = page.locator("#pane a:has-text('Скачать')")
    expect(link).to_be_visible()

    href = link.get_attribute("href")
    assert href.endswith("/download"), href      # по этому суффиксу оболочка macOS решает
    resp = page.request.get(href)
    assert resp.status == 200, resp.status
    assert "text/plain" in resp.headers["content-type"]
    disp = resp.headers["content-disposition"]
    assert "attachment" in disp and ".txt" in disp, disp
    # Имя не должно превращаться в простыню: у неразобранной встречи заголовок — это
    # первая фраза расшифровки целиком.
    assert len(disp) < 300, disp
    assert "Планёрка по крабам" in resp.text()   # содержимое — сама расшифровка
    assert "Смету пришлю в пятницу" in resp.text()


def test_a_missing_transcript_file_says_so_instead_of_serving_nothing(page: Page):
    """У второй записи файла на диске нет: скачивание обязано объяснить, а не отдать
    пустоту или 500."""
    page.goto("/meetings")
    page.locator(".mrow:has-text('Не разобранная планёрка')").click()
    pane = page.locator("#pane")
    # Файла нет -> и кнопки нет, вместо неё объяснение.
    expect(pane).to_contain_text("Файл расшифровки не найден")
    expect(pane.locator("a:has-text('Скачать')")).to_have_count(0)

    # Даже если позвать напрямую — 404 с внятным текстом, а не 500 и не пустой файл.
    mid = pane.locator("[data-meeting-id]").get_attribute("data-meeting-id")
    resp = page.request.get(f"/meeting/{mid}/download")
    assert resp.status == 404, resp.status
    assert "архив записей" in resp.text()


def test_splitter_reaches_past_the_middle_of_a_wide_window(page: Page):
    """The reading pane used to be capped at 900px, so on a big monitor the splitter
    stopped around the middle and would not go further left."""
    page.set_viewport_size({"width": 2200, "height": 900})
    page.goto("/mail/inbox")
    splitter = page.locator("#splitter")
    box = splitter.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
    page.mouse.down()
    page.mouse.move(700, box["y"] + 200)  # far left of the middle
    page.mouse.up()
    width = page.evaluate(
        "() => document.querySelector('.pane').getBoundingClientRect().width"
    )
    assert width > 1400, width


def test_splitter_leaves_the_list_column_usable(page: Page):
    page.set_viewport_size({"width": 1200, "height": 900})
    page.goto("/mail/inbox")
    box = page.locator("#splitter").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
    page.mouse.down()
    page.mouse.move(10, box["y"] + 200)  # drag past the left edge of the window
    page.mouse.up()
    list_width = page.evaluate(
        "() => document.querySelector('.main').getBoundingClientRect().width"
    )
    assert list_width >= 360, list_width


def test_meeting_task_links_to_the_moment_it_quotes(page: Page, click_and_settle):
    """«[00:23:10]» in the card means nothing on its own. The card has to lead to the
    recording, to the transcript file, and to the place in it that was quoted — the
    transcript is tens of thousands of characters, the top of it is not context."""
    page.goto("/view/today?account=")
    page.wait_for_load_state("networkidle")
    click_and_settle(page.locator(".trow:has-text('Подготовить смету') .trow-title"))
    block = page.locator("#pane .src-block")
    expect(block).to_contain_text("Планёрка по крабам")
    # О чём встреча — в самой карточке, без расшифровки.
    expect(block.locator(".src-sum")).to_have_text("Обсудили сроки поставки и смету.")
    href = block.locator("a.btn").get_attribute("href")
    assert href.endswith("/download"), href
    click_and_settle(block.locator("button:has-text('Открыть встречу')"))
    # The quoted second is not a stamp in the file; the turn that was running is.
    expect(page.locator("#tc-hit")).to_contain_text("Смету пришлю в пятницу")


# --- docs/ux-tasks.md §13: one sticky toolbar under the title; a selection swaps its
# content in place; grouping is a select; quick-add is a ghost row; the queue status is in
# the sidebar footer. None of these change the seeded data. ---


def test_toolbar_swaps_in_place_when_rows_are_checked(page: Page):
    """The strip changes content, the rows do not move — so a shift-click on the second
    row lands on the second row (the breakage measured in §13.1)."""
    page.goto("/view/today?account=")
    page.wait_for_load_state("networkidle")
    rows = page.locator("#list .trow[data-item-id]")
    assert rows.count() >= 2, page.locator("#list").inner_text()
    y_before = rows.nth(1).bounding_box()["y"]
    rows.nth(0).locator(".trow-check").click()
    expect(page.locator("#bulk-bar")).to_be_visible()
    expect(page.locator("#bulk-n")).to_have_text("1")
    expect(page.locator("#tools-main")).to_be_hidden()
    assert rows.nth(1).bounding_box()["y"] == y_before
    rows.nth(1).locator(".trow-check").click(modifiers=["Shift"])
    expect(page.locator("#bulk-n")).to_have_text("2")
    expect(page.locator("#list .trow.checked")).to_have_count(2)
    page.keyboard.press("Escape")
    expect(page.locator("#bulk-bar")).to_be_hidden()
    expect(page.locator("#tools-main")).to_be_visible()
    expect(page.locator("#list .trow.checked")).to_have_count(0)


def test_toolbar_sticks_while_the_list_scrolls(page: Page):
    page.set_viewport_size({"width": 1200, "height": 260})
    page.goto("/view/today?account=")
    page.wait_for_load_state("networkidle")
    content = page.locator(".content")
    content.evaluate("el => { el.scrollTop = 120; }")
    assert content.evaluate("el => el.scrollTop") > 0
    tools = page.locator("#board-tools").bounding_box()
    top = content.bounding_box()["y"]
    assert abs(tools["y"] - top) <= 1, (tools, top)


def test_grouping_is_a_select_in_the_toolbar(page: Page):
    page.goto("/view/today?account=")
    page.wait_for_load_state("networkidle")
    page.locator("#board-tools .tools-group").select_option("person")
    expect(page.locator("#list .tsec.named").first).to_be_visible()
    page.locator("#board-tools .tools-group").select_option("")
    expect(page.locator("#list .tsec.named")).to_have_count(0)


def test_quick_add_is_a_ghost_row_shown_where_tasks_land(page: Page):
    page.goto("/view/needreply?account=")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#quick-add")).to_be_hidden()
    page.locator("#board-tools [data-quick-add]").click()
    expect(page.locator("#quick-add .qadd-input")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator("#quick-add")).to_be_hidden()
    page.goto("/view/today?account=")
    expect(page.locator("#quick-add")).to_be_visible()
    # System state is in the sidebar footer, not a strip over the board.
    expect(page.locator("#sidebar #agent-status")).to_contain_text("очередь")
    expect(page.locator("#board-head #agent-bar")).to_have_count(0)


# --- Stage 3/4 of docs/ux-tasks.md: quick-add and undo. Both change the seeded data, so
# they stay at the end of the file (pytest runs a module top to bottom). ---


def test_quick_add_puts_a_dated_task_on_my_tasks(page: Page):
    page.goto("/view/commitments?account=")
    page.wait_for_load_state("networkidle")
    box = page.locator("#quick-add .qadd-input")
    box.fill("проверить отчёт по конверсии завтра")
    box.press("Enter")
    row = page.locator(".trow:has-text('проверить отчёт по конверсии')")
    expect(row).to_be_visible()
    expect(row.locator(".trow-date")).to_have_text("завтра")
    expect(box).to_have_value("")  # the box empties for the next line
    expect(box).to_be_focused()  # …and keeps the focus, though the list re-rendered
    # The date phrase did not leak into the title.
    expect(row.locator(".trow-title")).not_to_contain_text("завтра")


def test_undo_brings_a_completed_row_back(page: Page):
    page.goto("/view/today?account=")
    page.wait_for_load_state("networkidle")
    row = page.locator(".trow:has-text('Подготовить смету')")
    row.hover()
    row.locator(".act.ok").click()
    expect(page.locator("#list")).not_to_contain_text("Подготовить смету")
    toast = page.locator(".toast:has-text('Готово')")
    expect(toast).to_be_visible()
    toast.locator(".toast-undo").click()
    expect(page.locator("#list")).to_contain_text("Подготовить смету")


# --- Meetings across spaces (docs/ux-tasks.md §15): delete with undo, move from the pane
# menu. Both change the seeded data, so they stay last. ---


def test_deleting_a_meeting_is_undoable(page: Page, click_and_settle):
    page.goto("/meetings")
    row = page.locator(".mrow:has-text('Не разобранная планёрка')")
    row.click(button="right")
    menu = page.locator("#ctx-menu")
    expect(menu).to_contain_text("Перенести в «Дом»")
    expect(menu).to_contain_text("Удалить встречу")
    page.keyboard.press("Escape")
    page.mouse.click(5, 5)
    click_and_settle(row)
    page.locator("#pane [data-meeting-delete]").click()
    expect(page.locator("#list")).not_to_contain_text("Не разобранная планёрка")
    expect(page.locator("#pane")).to_contain_text("Восстановить")
    toast = page.locator(".toast:has-text('удалена')")
    expect(toast).to_be_visible()
    toast.locator(".toast-undo").click()
    expect(page.locator("#list")).to_contain_text("Не разобранная планёрка")
    expect(page.locator("#pane")).not_to_contain_text("Восстановить")


def test_meeting_moves_between_spaces_from_the_pane_menu(page: Page, click_and_settle):
    page.goto("/meetings")
    click_and_settle(page.locator(".mrow:has-text('Планёрка по крабам')"))
    page.locator("#pane [data-space-menu]").click()
    page.locator("#ctx-menu .ctx-item:has-text('Дом')").click()
    expect(page.locator("#list")).not_to_contain_text("Планёрка по крабам")
    expect(page.locator("#pane")).to_contain_text("в пространстве «Дом»")
    # Its task went with it: no longer on this space's «Сегодня».
    page.goto("/view/today?account=")
    expect(page.locator("#list")).not_to_contain_text("Подготовить смету")
    # Over in «Дом», move it back the same way; «Работа» lists it again.
    page.select_option("#space-switch select", label="Дом")
    page.wait_for_load_state("networkidle")
    page.goto("/meetings")
    click_and_settle(page.locator(".mrow:has-text('Планёрка по крабам')"))
    page.locator("#pane [data-space-menu]").click()
    page.locator("#ctx-menu .ctx-item:has-text('Работа')").click()
    expect(page.locator("#list")).not_to_contain_text("Планёрка по крабам")
    page.select_option("#space-switch select", label="Работа")
    page.wait_for_load_state("networkidle")
    page.goto("/meetings")
    expect(page.locator("#list")).to_contain_text("Планёрка по крабам")


def test_snoozing_from_the_pane_menu_takes_the_row_off_today(page: Page, click_and_settle):
    page.goto("/view/today?account=")
    page.wait_for_load_state("networkidle")
    click_and_settle(page.locator(".trow:has-text('Согласовать график поставок')"))
    pane = page.locator("#pane")
    expect(pane.locator("[data-due-btn]")).to_have_class(re.compile("due-over"))  # overdue, red
    pane.locator("[data-snooze-menu]").click()
    page.locator("#ctx-menu .ctx-item:has-text('На 3 дня')").click()
    expect(page.locator("#list")).not_to_contain_text("Согласовать график поставок")
    page.goto("/view/snoozed?account=")
    expect(page.locator("#list")).to_contain_text("Согласовать график поставок")


def test_deleting_a_meeting_from_a_task_pane_keeps_the_board(page: Page, click_and_settle):
    """The meeting pane reached through a task on «Сегодня»: deleting must not swap the
    meetings list into the board. The board stays, minus the meeting's task; undo brings
    the row back."""
    page.goto("/view/today?account=")
    page.wait_for_load_state("networkidle")
    click_and_settle(page.locator(".trow:has-text('Подготовить смету') .trow-title"))
    click_and_settle(page.locator("#pane button:has-text('Открыть встречу')"))
    expect(page.locator("#pane")).to_contain_text("Планёрка по крабам")
    page.locator("#pane [data-meeting-delete]").click()
    expect(page.locator("#pane")).to_contain_text("Восстановить")
    board = page.locator("#list")
    expect(board).not_to_contain_text("Подготовить смету")
    expect(board.locator("#board-tools")).to_be_visible()  # still the board…
    expect(board.locator("#meeting-list")).to_have_count(0)  # …not the meetings list
    page.locator(".toast:has-text('удалена') .toast-undo").click()
    expect(board).to_contain_text("Подготовить смету")
    expect(page.locator("#pane")).not_to_contain_text("Восстановить")


def test_deleting_a_meeting_from_the_project_page_takes_its_row(page: Page, click_and_settle):
    """The other page that opens a meeting pane without the meetings list: the project's
    own meeting rows are re-read, so the deleted one leaves them (and comes back on undo)."""
    # The move test above took the meeting out of «Поставки» (a move drops the old space's
    # memberships); file it back by hand from its pane, the way the docs say to.
    page.goto("/meetings")
    click_and_settle(page.locator(".mrow:has-text('Планёрка по крабам')"))
    if page.locator("#pane .proj-chip:has-text('Поставки')").count() == 0:
        page.select_option("#pane select[name=project_id]", label="Поставки")
        click_and_settle(page.locator("#pane button:has-text('Добавить')"))
    expect(page.locator("#pane .proj-chip:has-text('Поставки')")).to_be_visible()
    page.goto("/projects")
    page.locator(".acc-row:has-text('Поставки') a:has-text('Открыть')").click()
    row = page.locator("#list .mrow:has-text('Планёрка по крабам')")
    click_and_settle(row)
    page.locator("#pane [data-meeting-delete]").click()
    expect(page.locator("#pane")).to_contain_text("Восстановить")
    expect(row).to_have_count(0)
    expect(page.locator("#list")).to_contain_text("всё про поставки")  # still the project page
    page.locator(".toast:has-text('удалена') .toast-undo").click()
    expect(row).to_have_count(1)


# --- «Люди» (docs/feature-people.md): один список отовсюду, память без адреса,
# объединение личностей. Меняют посев, поэтому идут последними. ---


def test_people_screen_lists_everyone_the_space_has_seen(page: Page, click_and_settle):
    page.goto("/people")
    page.wait_for_load_state("networkidle")
    rows = page.locator("#people-list .prow")
    expect(rows).to_have_count(4)
    # Человек со встречи — в списке, хотя адреса у него нет.
    meeting_row = page.locator(".prow:has-text('со встреч')")
    expect(meeting_row).to_contain_text("Иванов")
    expect(meeting_row).to_contain_text("только имя")
    # Граница пространства: в «Доме» этих людей нет — список строится из его источников.
    # Переключатель отправляет форму, поэтому ждём саму навигацию, а не «сеть затихла».
    with page.expect_navigation():
        page.select_option("#space-switch select", label="Дом")
    expect(page.locator("#list")).not_to_contain_text("Иванов")
    expect(page.locator("#list")).not_to_contain_text("Bob")
    with page.expect_navigation():
        page.select_option("#space-switch select", label="Работа")


def test_people_search_keeps_the_cursor_while_typing(page: Page):
    """Поле поиска перерисовывалось вместе со списком: после первой буквы фокус пропадал,
    и следующие буквы уходили в никуда."""
    page.goto("/people")
    page.wait_for_load_state("networkidle")
    box = page.locator("#people-tools .tools-search")
    box.click()
    # Пауза длиннее задержки поиска: список перерисовывается после каждой буквы.
    box.press_sequentially("krab", delay=400)
    page.wait_for_load_state("networkidle")

    expect(box).to_be_focused()
    expect(box).to_have_value("krab")
    expect(page.locator("#people-list .prow")).to_have_count(1)
    expect(page.locator("#people-count")).to_have_text("1")


def test_a_person_without_an_address_can_be_remembered(page: Page, click_and_settle):
    """Раньше «Память» отказывалась работать без e-mail, и человека со встречи запомнить
    было нельзя (docs/feature-people.md §1)."""
    page.goto("/people")
    click_and_settle(page.locator(".prow:has-text('со встреч')"))
    pane = page.locator("#pane")
    expect(pane).to_contain_text("Почта к профилю не привязана")
    pane.locator("summary:has-text('Добавить, что я знаю')").click()
    pane.locator("textarea[name=notes]").fill("Ведёт поставки, звонить после обеда")
    pane.locator("#person-memory button:has-text('Сохранить')").click()
    expect(pane.locator("textarea[name=notes]")).to_have_value(
        "Ведёт поставки, звонить после обеда"
    )
    # Заметка осталась на профиле: строка списка теперь помечена.
    page.goto("/people")
    page.wait_for_load_state("networkidle")
    expect(page.locator(".prow:has-text('со встреч')")).to_contain_text("Иванов")


def test_two_rows_about_one_person_merge_and_unmerge(page: Page, click_and_settle):
    page.goto("/people")
    page.wait_for_load_state("networkidle")
    page.locator(".prow:has-text('со встреч') .prow-check").click()
    page.locator(".prow:has-text('ivanov@krab.ru') .prow-check").click()
    expect(page.locator("#people-sel")).to_be_visible()
    page.locator("#people-sel [data-people='merge']").click()

    expect(page.locator("#people-list .prow")).to_have_count(3)
    merged = page.locator(".prow:has-text('ivanov@krab.ru')")
    expect(merged).to_contain_text("со встреч")  # обе истории теперь у одного профиля
    toast = page.locator(".toast:has-text('Объединено')")
    expect(toast).to_be_visible()

    # Панель объединённого: обе личности с крестиком отвязки, обязательство со встречи.
    click_and_settle(merged)
    pane = page.locator("#pane")
    expect(pane.locator(".idchip:has-text('ivanov@krab.ru')")).to_be_visible()
    expect(pane.locator(".idchip:has-text('Иванов') .chip-x")).to_have_count(1)
    expect(pane).to_contain_text("Подготовить смету")

    toast.locator(".toast-undo").click()
    expect(page.locator("#people-list .prow")).to_have_count(4)


def test_a_normal_mailbox_still_offers_sync(page: Page):
    """Ту же кнопку у обычного ящика правка трогать не должна."""
    page.goto("/view/today")
    page.goto(page.locator(".acct-group a[href$='/inbox']").first.get_attribute("href"))
    expect(page.locator("#list .tools button:has-text('Синхронизировать')")).to_be_visible()


def test_every_page_renders_its_own_sidebar(page: Page):
    """Меню дёргалось на каждом переходе: страница отдавала неполное меню (без ящиков,
    иногда без счётчиков), htmx по `load` тут же подменял его полным, и было видно, как
    оно прыгает. На досках подмена совпадала — там и не дёргалось.

    Блокируем /ui/sidebar: то, что пришло со страницей, должно быть уже правильным."""
    page.route("**/ui/sidebar", lambda route: route.abort())

    urls = ["/view/today", "/view/done", "/mail/inbox", "/meetings", "/people",
            "/projects", "/review", "/settings/section/spaces"]
    for url in urls:
        page.goto(url)
        sidebar = page.locator("#sidebar")
        expect(sidebar.locator(".acct-group")).to_have_count(1, timeout=3000)
        expect(sidebar.locator("#space-switch")).to_be_visible()
        expect(sidebar.locator(".nav-item")).not_to_have_count(0)


def test_mail_grouping_is_a_setting_and_it_works(page: Page, click_and_settle):
    """Пункт настроек, а не жёсткое поведение: список писем либо плоский, либо по одной
    строке на переписку."""
    page.goto("/settings/section/general")
    box = page.locator("input[name=grouped]")
    expect(box).not_to_be_checked()
    box.check()
    click_and_settle(page.locator("button:has-text('Сохранить')").last, "#mail-grouping-status")
    expect(page.locator("#mail-grouping-status")).to_contain_text("Группировать по теме")

    page.goto("/mail/inbox")
    expect(page.locator(".mlist")).to_be_visible()

    page.goto("/settings/section/general")
    page.locator("input[name=grouped]").uncheck()
    click_and_settle(page.locator("button:has-text('Сохранить')").last, "#mail-grouping-status")
    expect(page.locator("#mail-grouping-status")).to_contain_text("без группировки")


def test_the_mail_toolbar_stays_put_and_speaks_in_icons(page: Page):
    """«Написать» уезжала вверх при прокрутке — теперь она в шапке, рядом с поиском,
    а шапка не прокручивается вовсе. Действия письма — иконками, а не юникод-глифами."""
    page.goto("/mail/inbox")
    expect(page.locator(".main-head button[title='Написать письмо'] svg")).to_be_visible()
    # В общем списке пространства полосе инструментов нечего показывать — её и нет.
    expect(page.locator("#list .tools")).to_have_count(0)

    page.goto(page.locator(".acct-group a[href$='/inbox']").first.get_attribute("href"))
    tools = page.locator("#list .tools")
    expect(tools).to_be_visible()
    assert tools.evaluate("el => getComputedStyle(el).position") == "sticky"

    page.locator(".mrow").first.click()
    actions = page.locator(".read-actions")
    expect(actions.locator(".seg .btn")).to_have_count(3)
    for label in ("Ответить", "Ответить всем", "Переслать"):
        expect(actions.locator(f"button[title='{label}'] svg")).to_be_visible()
    assert "↩" not in actions.inner_text()


def test_recipient_autocomplete_fills_the_address(page: Page):
    """Адрес в «Кому» подбирается по первым буквам имени или почты: список приходит с
    сервера, выбор — стрелками и Enter, подстановка идёт в последнюю позицию списка."""
    page.goto("/mail/inbox")
    page.locator(".main-head button[title='Написать письмо']").click()
    to = page.locator("input[name=to]")
    expect(to).to_be_visible()

    to.click()
    to.type("bob", delay=30)
    items = page.locator("input[name=to] ~ .ac-drop .ac-item")
    expect(items.first).to_be_visible(timeout=3000)

    to.press("ArrowDown")
    to.press("Enter")
    expect(to).to_have_value(re.compile(r"@"))
    # Enter выбрал подсказку, а не отправил письмо.
    expect(page.locator(".compose-form")).to_be_visible()

    # Второй адрес дописывается через запятую, а не затирает первый.
    first = to.input_value()
    to.type("bob", delay=30)
    expect(items.first).to_be_visible(timeout=3000)
    to.press("Enter")
    assert to.input_value().startswith(first)


def test_a_collapsed_mailbox_stays_collapsed_without_the_menu_jumping(page: Page):
    """Свёрнутые ящики лежат в localStorage, сервер о них не знает. Страница приходила с
    развёрнутыми группами, а схлопывались они позже — по htmx-подмене сайдбара, и меню
    на глазах уезжало вверх. Теперь состояние применяется до первой отрисовки."""
    page.goto("/view/today")
    cap = page.locator("[data-acct-toggle]").first
    acct = cap.get_attribute("data-acct-toggle")
    group = page.locator(f'[data-acct-group="{acct}"]')
    expect(group).to_be_visible()
    cap.click()
    expect(group).to_be_hidden()

    # Полная перезагрузка со снятым ui.js: страница обязана прийти уже свёрнутой. Без
    # блокировки проверка бессмысленна — ui.js схлопнет группу сам, но уже после
    # отрисовки, и именно этот скачок человек и видит.
    page.route("**/ui.js", lambda route: route.abort())
    page.goto("/mail/inbox")
    assert page.locator(f'[data-acct-group="{acct}"]').evaluate(
        "el => el.style.display"
    ) == "none"
    expect(page.locator(f'[data-acct-toggle="{acct}"] .acct-arrow')).to_have_text("▸")
    page.unroute("**/ui.js")

    # Подмена сайдбара (countsChanged) ничего не двигает — она уже совпадает со страницей.
    page.goto("/mail/inbox")  # заново, уже со скриптами
    page.locator(".mrow").first.click()
    expect(page.locator("#pane .read-subject")).to_be_visible()
    expect(page.locator(f'[data-acct-group="{acct}"]')).to_be_hidden()

    cap = page.locator(f'[data-acct-toggle="{acct}"]')
    cap.click()  # вернуть как было
    expect(page.locator(f'[data-acct-group="{acct}"]')).to_be_visible()


def test_the_menu_keeps_its_scroll_between_folders(page: Page):
    """Каждый пункт меню — обычная ссылка, то есть полная перезагрузка. Список ящиков
    длиннее окна, и меню всякий раз отбрасывало в начало."""
    page.set_viewport_size({"width": 1200, "height": 400})  # чтобы меню точно не влезло
    page.goto("/mail/inbox")
    nav = page.locator(".sidebar .nav")
    assert nav.evaluate("el => el.scrollHeight > el.clientHeight"), "меню и так помещается"

    nav.evaluate("el => { el.scrollTop = 120; el.dispatchEvent(new Event('scroll')); }")
    page.goto(page.locator(".acct-group a[href$='/sent']").first.get_attribute("href"))

    assert page.locator(".sidebar .nav").evaluate("el => el.scrollTop") > 0


def test_a_conversation_opens_in_place_by_the_arrow(page: Page):
    """Группировка по теме даёт одну строку на беседу. Стрелка раскрывает её письма
    прямо в списке, как в почтовых клиентах, — и повторное нажатие прячет их."""
    page.goto("/settings/section/general")
    page.locator("input[name=grouped]").check()
    page.locator("button:has-text('Сохранить')").last.click()
    expect(page.locator("#mail-grouping-status")).to_contain_text("Группировать")

    page.goto("/mail/inbox")
    row = page.locator(".mrow", has_text="Смета на кухню").first
    arrow = row.locator(".mrow-exp")
    expect(arrow.locator(".mrow-exp-n")).to_have_text("2")  # счётчик внутри той же кнопки
    expect(arrow).to_be_visible()
    kids = page.locator('.mrow-kids[data-kids]').first
    expect(kids).to_be_hidden()

    arrow.click()
    expect(kids.locator(".mrow.sub").first).to_be_visible(timeout=3000)
    expect(arrow).to_have_attribute("aria-expanded", "true")
    # Клик по стрелке не открывает письмо в панели — это разные действия.
    expect(page.locator("#pane .read-subject")).to_have_count(0)

    # Клик по письму внутри группы открывает СПРАВА именно его.
    second = kids.locator(".mrow.sub").nth(1)
    expect(second).to_be_visible()
    want = second.locator(".mrow-prev").inner_text()
    second.click()
    expect(page.locator("#pane .read-body")).to_contain_text(want[:20], timeout=3000)

    # Вложенные письма сдвинуты вправо — и прочитанные, и непрочитанные.
    pads = kids.locator(".mrow.sub").evaluate_all(
        "els => els.map(el => parseFloat(getComputedStyle(el).paddingLeft))"
    )
    assert pads and all(p >= 32 for p in pads), pads

    arrow.click()
    expect(kids).to_be_hidden()

    page.goto("/settings/section/general")
    page.locator("input[name=grouped]").uncheck()
    page.locator("button:has-text('Сохранить')").last.click()
    expect(page.locator("#mail-grouping-status")).to_contain_text("без группировки")


def test_a_letter_inside_a_commitment_is_rendered_not_flattened(page: Page):
    """В карточке обязательства письмо показывалось плоским текстом: таблицы и списки
    рассыпались в столбик и не читались. Теперь та же песочница, что в почте."""
    page.goto("/view/commitments?account=")
    page.locator(".trow:has-text('перечитать письмо с таблицей')").click()
    msg = page.locator("#pane .msg").first
    expect(msg).to_be_visible()

    frame = msg.locator("iframe.msg-html")
    expect(frame).to_be_visible()
    assert "/html" in frame.get_attribute("src")
    expect(frame.content_frame.locator("table")).to_be_visible()

    # Переключение на текст остаётся, и клик по нему не открывает письмо целиком.
    msg.locator("button:has-text('Только текст')").click()
    expect(frame).to_be_hidden()
    expect(msg.locator(".msg-body")).to_be_visible()


def test_recipient_fields_are_as_wide_as_the_subject(page: Page):
    """Обёртка для подсказки адресов лишила «Кому» и «Копия» ширины: поля стали узкими,
    а «Тема» рядом — во всю форму."""
    page.goto("/mail/inbox")
    page.locator(".main-head button[title='Написать письмо']").click()
    expect(page.locator(".compose-form")).to_be_visible()

    widths = page.locator("input[name=to], input[name=cc], input[name=subject]").evaluate_all(
        "els => els.map(el => Math.round(el.getBoundingClientRect().width))"
    )
    to, cc, subject = widths
    assert abs(to - subject) <= 2 and abs(cc - subject) <= 2, widths


def test_the_compose_field_uses_the_font_the_letter_will_be_sent_in(page: Page):
    """Письмо уходит шрифтом подписи (у корпоративной почты это Calibri 11pt), а поле
    ввода показывало системный: человек набирал одно, получатель видел другое."""
    page.goto("/mail/inbox")
    page.locator(".main-head button[title='Написать письмо']").click()
    body = page.locator("#compose-body")
    expect(body).to_be_visible()

    font = body.evaluate("el => getComputedStyle(el).fontFamily.toLowerCase()")
    size = body.evaluate("el => getComputedStyle(el).fontSize")
    assert "calibri" in font, font
    assert size == "14.6667px", size  # 11pt


def test_cancelling_an_untouched_composer_leaves_no_draft(page: Page):
    """Каждое «Ответить → Отмена» иначе оставляло бы черновик из одной подписи."""
    page.goto("/mail/inbox")
    page.locator(".main-head button[title='Написать письмо']").click()
    expect(page.locator(".compose-form")).to_be_visible()
    page.locator(".wz-actions button:has-text('Отмена')").click()
    expect(page.locator(".compose-form")).to_have_count(0)
    page.wait_for_load_state("networkidle")
    expect(page.locator(".acct-group a[href$='/drafts']")).to_have_count(0)


def test_deleting_an_open_draft_closes_its_composer(page: Page, click_and_settle):
    """Удалённый черновик нельзя оставлять открытым: автосохранение по нему больше не
    проходит, и всё, что человек допишет после удаления, пропало бы молча."""
    page.goto("/view/today")
    page.select_option("#space-switch select", label="Дом")
    inbox = page.locator(".acct-group a[href$='/inbox']").first.get_attribute("href")
    page.goto(inbox)

    page.locator(".main-head button[title='Написать письмо']").click()
    expect(page.locator(".compose-form")).to_be_visible()
    page.fill(".compose-form input[name=subject]", "Лишний черновик")
    page.locator(".wz-actions button:has-text('Отмена')").click()

    drafts = page.locator(".acct-group a[href$='/drafts']")
    expect(drafts).to_be_visible()
    drafts.click()
    row = page.locator(".mrow", has_text="Лишний черновик")
    click_and_settle(row)
    expect(page.locator(".compose-form")).to_be_visible()

    page.on("dialog", lambda d: d.accept())
    click_and_settle(row.locator("button:has-text('Удалить')"), "#draft-list")
    expect(page.locator(".mrow", has_text="Лишний черновик")).to_have_count(0)
    expect(page.locator(".compose-form")).to_have_count(0)
    expect(page.locator(".acct-group a[href$='/drafts']")).to_have_count(0)
    page.select_option("#space-switch select", label="Работа")


def test_a_draft_survives_leaving_by_a_menu_link(page: Page):
    """«Бросил письмо» — это чаще уход по ссылке в меню, а не «Отмена»: страница уносит
    с собой и таймер автосохранения, и незавершённый запрос."""
    page.goto("/view/today")
    page.select_option("#space-switch select", label="Дом")
    inbox = page.locator(".acct-group a[href$='/inbox']").first.get_attribute("href")
    page.goto(inbox)

    page.locator(".main-head button[title='Написать письмо']").click()
    expect(page.locator(".compose-form")).to_be_visible()
    # Набор клавишами, а не fill: живой человек уходит со страницы, не покидая поля, и
    # события `change` не будет — сохранить письмо должна сама выгрузка страницы.
    page.locator("#compose-body").press_sequentially("Ушёл по ссылке")
    page.goto(inbox.replace("/inbox", "/sent"))

    # Страница сохраняет письмо уже на выходе, и следующая успевает отрисоваться
    # раньше: важно, что текст цел, а меню догоняет на первом же переходе.
    page.goto(inbox)
    drafts = page.locator(".acct-group a[href$='/drafts']")
    expect(drafts).to_be_visible()
    drafts.click()
    row = page.locator(".mrow", has_text="Ушёл по ссылке")
    expect(row).to_be_visible()

    page.on("dialog", lambda d: d.accept())
    row.locator("button:has-text('Удалить')").click()
    expect(page.locator(".mrow", has_text="Ушёл по ссылке")).to_have_count(0)
    page.select_option("#space-switch select", label="Работа")


def test_a_letter_still_being_written_is_already_saved(page: Page):
    """Автосохранение не должно ждать конца набора: сбрасываемый каждой клавишей таймер
    при непрерывном письме не срабатывал бы вовсе."""
    page.goto("/view/today")
    page.select_option("#space-switch select", label="Дом")
    inbox = page.locator(".acct-group a[href$='/inbox']").first.get_attribute("href")
    page.goto(inbox)

    page.locator(".main-head button[title='Написать письмо']").click()
    expect(page.locator(".compose-form")).to_be_visible()
    # Набираем дольше, чем период автосохранения, и не покидаем поле: к концу набора
    # черновик должен быть уже сохранён, а не ждать паузы.
    page.locator("#compose-body").press_sequentially(
        "Пишу и не останавливаюсь", delay=200
    )
    expect(page.locator(".acct-group a[href$='/drafts']")).to_be_visible(timeout=1000)

    # В черновике то, что набрано к моменту сохранения, — целиком письмо туда попадёт
    # на следующем срабатывании или при уходе со страницы.
    page.goto(inbox.replace("/inbox", "/drafts"))
    row = page.locator(".mrow", has_text="Пишу и")
    expect(row).to_be_visible()
    page.on("dialog", lambda d: d.accept())
    row.locator("button:has-text('Удалить')").click()
    expect(page.locator(".mrow", has_text="Пишу и")).to_have_count(0)
    page.select_option("#space-switch select", label="Работа")


def test_a_label_marks_the_letter_from_that_address_only(page: Page):
    """Правило заводят на адрес: метка должна стоять на письме этого человека, а не на
    всей переписке, в которой он написал."""
    page.goto("/mail/inbox")
    row = page.locator(".mrow", has_text="Доступы к API").first
    arrow = row.locator(".mrow-exp")
    if arrow.count():  # список сгруппирован — раскрываем беседу
        arrow.click()
        kids = page.locator(".mrow-kids[data-kids]").filter(has_text="Доступы к API")
        rows = kids.locator(".mrow.sub")
    else:
        rows = page.locator(".mrow", has_text="Доступы к API")
    expect(rows.first).to_be_visible(timeout=3000)
    expect(rows).to_have_count(3)
    # Чип ровно один — на письме Бориса; у Антона его нет.
    expect(rows.locator(".lbl-chip")).to_have_count(1)
    expect(rows.filter(has_text="boris").locator(".lbl-chip")).to_have_text("БЛ")
    expect(rows.filter(has_text="anton").locator(".lbl-chip")).to_have_count(0)

    # Отбор по метке показывает письма с меткой, а не беседы, в которых она встретилась.
    page.goto("/settings/section/labels")
    label_id = page.locator("select[name=label_id] option:has-text('БЛ')").get_attribute("value")
    page.goto(f"/mail/inbox?label={label_id}")
    expect(page.locator(".main-head h1")).to_contain_text("Метка: БЛ")
    listed = page.locator(".mrow")
    expect(listed).to_have_count(1)
    expect(listed.first).to_contain_text("boris")


# --- История карточки (docs/ux-tasks.md §17) ---


def test_the_card_tells_its_own_history_without_shouting(page: Page, click_and_settle):
    """Тихо: блок свёрнут, на доске ни значка, ни счётчика. Внутри — что сделал ассистент,
    и снятие с доски не выдаётся за исполнение."""
    page.goto("/view/commitments?account=")
    page.wait_for_load_state("networkidle")
    row = page.locator(".trow:has-text('перечитать письмо с таблицей')")
    # На строке ничего про историю нет — смена типа пользователю ничего не стоит.
    assert "История" not in (row.inner_text() or "")
    click_and_settle(row.locator(".trow-title"))
    hist = page.locator("#pane details.hist")
    expect(hist.locator("summary")).to_contain_text("История")
    assert hist.evaluate("el => el.open") is False  # свёрнуто по умолчанию
    hist.locator("summary").click()
    expect(page.locator("#pane .hist-list")).to_be_visible()
    rows = page.locator("#pane .hist-row")
    expect(rows).to_have_count(3)
    expect(rows.nth(0)).to_contain_text("Жду ответа → Моё обещание: пришёл ответ и новый вопрос")
    # Снятие не должно читаться как закрытие: свой текст и своя приглушённая роль.
    expect(rows.nth(1)).to_contain_text("Снята с доски")
    expect(rows.nth(1)).not_to_contain_text("Закрыта")
    expect(rows.nth(1)).to_have_class(re.compile("drop"))
    expect(rows.nth(2)).to_contain_text("Снова открыта")


def test_closing_a_card_by_hand_shows_up_in_its_history(page: Page, click_and_settle):
    page.goto("/view/commitments?account=")
    page.wait_for_load_state("networkidle")
    click_and_settle(page.locator(".trow:has-text('Сверить счёт с договором') .trow-title"))
    page.locator("#pane [data-more-menu]").click()
    page.locator("#ctx-menu .ctx-item:has-text('Не обязательство')").click()
    page.goto("/view/done?account=")
    page.wait_for_load_state("networkidle")
    click_and_settle(page.locator(".trow:has-text('Сверить счёт с договором') .trow-title"))
    page.locator("#pane details.hist summary").click()
    expect(page.locator("#pane .hist-row")).to_contain_text("Не обязательство")
    expect(page.locator("#pane .hist-row.user")).to_have_count(1)


def test_a_stale_card_is_offered_for_closing_and_i_can_keep_it(page: Page):
    """Секция «Предлагаю закрыть» внизу своей доски: предложение, не действие. «Оставить»
    возвращает строку в обычную секцию (docs/ux-tasks.md §17.4)."""
    page.goto("/view/commitments?account=")
    page.wait_for_load_state("networkidle")
    board = page.locator("#list")
    expect(board).to_contain_text("Предлагаю закрыть")
    # Предложение — последняя секция: это уборка, а не срочное.
    heads = board.locator(".tsec")
    expect(heads.last).to_contain_text("Предлагаю закрыть")
    row = board.locator(".trow:has-text('Уточнить смету у подрядчика')")
    expect(row.locator(".tag.dot")).to_have_attribute(
        "title", re.compile("Предлагаю закрыть: давно ничего не происходит")
    )
    row.hover()
    row.locator("button[title^='Оставить открытым']").click()
    expect(board.locator(".trow:has-text('Уточнить смету у подрядчика') .tag.dot")).to_have_count(0)
    expect(board).not_to_contain_text("Предлагаю закрыть")


# --- Настраиваемые доски (docs/ux-tasks.md §17.5) ---


def test_hiding_a_board_takes_it_out_of_the_menu_but_not_out_of_the_app(page: Page):
    page.goto("/settings/section/boards")
    page.wait_for_load_state("networkidle")
    row = page.locator(".board-row:has(input[value='Разобрать'])")
    row.locator("input[name=hidden]").check()
    row.locator("button:has-text('Сохранить')").click()
    page.wait_for_load_state("networkidle")
    page.goto("/view/today?account=")
    expect(page.locator(".nav")).not_to_contain_text("Разобрать")
    # Сохранённая ссылка продолжает работать — доска спрятана, а не удалена.
    page.goto("/view/triage")
    expect(page.locator(".main-head h1")).to_contain_text("Разобрать")
    # Вернуть на место, чтобы не мешать другим проверкам меню.
    page.goto("/settings/section/boards")
    row = page.locator(".board-row:has(input[value='Разобрать'])")
    row.locator("input[name=hidden]").uncheck()
    row.locator("button:has-text('Сохранить')").click()
    page.wait_for_load_state("networkidle")


def test_a_board_i_built_myself_shows_what_i_asked_for(page: Page):
    page.goto("/settings/section/boards")
    page.wait_for_load_state("networkidle")
    page.locator("summary:has-text('Новая доска')").click()
    form = page.locator("form[hx-post='/settings/boards-new']")
    form.locator("input[name=name]").fill("Только мои задачи")
    form.locator("input[name=types][value='TASK']").check()
    form.locator("select[name=sort]").select_option("created_asc")
    form.locator("button:has-text('Создать')").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator(".board-row input[value='Только мои задачи']")).to_have_count(1)

    # Доска появилась в меню и показывает ровно задачи — не обязательства.
    page.goto("/view/today?account=")
    nav = page.locator(".nav .nav-item:has-text('Только мои задачи')")
    expect(nav).to_have_count(1)
    nav.click()
    page.wait_for_load_state("networkidle")
    expect(page.locator(".main-head h1")).to_contain_text("Только мои задачи")
    board = page.locator("#list")
    expect(board).to_contain_text("Уточнить смету у подрядчика")
    expect(board).not_to_contain_text("перечитать письмо с таблицей")

    # Удаление уносит отбор, а не карточки. У кнопки hx-confirm — подтверждаем.
    page.on("dialog", lambda d: d.accept())
    page.goto("/settings/section/boards")
    page.locator(
        ".board-row:has(input[value='Только мои задачи']) button:has-text('Удалить')"
    ).click()
    page.wait_for_load_state("networkidle")
    expect(page.locator(".board-row input[value='Только мои задачи']")).to_have_count(0)
    page.goto("/view/commitments?account=")
    expect(page.locator("#list")).to_contain_text("Уточнить смету у подрядчика")


def test_dragging_a_row_onto_a_board_still_retypes_it(page: Page):
    """`data-drop` теперь несёт тип, а не ключ доски (§17.5) — перенос мышью должен
    продолжать работать и на пресете, и на своей доске."""
    page.goto("/view/today?account=")
    page.wait_for_load_state("networkidle")
    waiting = page.locator(".nav-item:has-text('Жду ответа')")
    expect(waiting).to_have_attribute("data-drop", "WAITING_FOR_REPLY")
    expect(page.locator(".nav-item:has-text('Делегировано')")).to_have_attribute(
        "data-drop", "DELEGATED_TASK"
    )
    row = page.locator(".trow:has-text('Уточнить смету у подрядчика')")
    if row.count() == 0:
        page.goto("/view/commitments?account=")
        row = page.locator(".trow:has-text('Уточнить смету у подрядчика')")
    row.drag_to(waiting)
    page.wait_for_load_state("networkidle")
    page.goto("/view/waiting?account=")
    expect(page.locator("#list")).to_contain_text("Уточнить смету у подрядчика")


# --- Новая задача: срок при создании и место, куда она попала (docs/ux-tasks.md §17.7) ---
# Набранная задача уезжала на «Мои задачи» в самый низ, на доску, которую пользователь не
# открывал, и находилась только поиском. Эти проверки — про то, что её видно.


def test_a_task_typed_on_today_stays_on_today(page: Page):
    """«Сегодня» подставляет сегодняшний срок: иначе задача, набранная здесь, исчезает
    с экрана в ту же секунду."""
    page.goto("/view/today?account=")
    page.wait_for_load_state("networkidle")
    btn = page.locator("#quick-add [data-qadd-due]")
    expect(btn).to_have_class(re.compile(r"\bset\b"))
    expect(btn.locator(".qadd-due-lbl")).to_have_text("сегодня")
    box = page.locator("#quick-add .qadd-input")
    box.fill("позвонить в сервис")
    box.press("Enter")
    row = page.locator(".trow:has-text('позвонить в сервис')")
    expect(row).to_be_visible()
    expect(row.locator(".trow-date")).to_have_text("сегодня")
    # Строка не просто есть — на неё наведён курсор списка, чтобы её нашёл взгляд.
    expect(row).to_have_class(re.compile(r"\bcur\b"))


def test_the_date_button_sets_the_due_date_before_the_card_exists(page: Page):
    page.goto("/view/commitments?account=")
    page.wait_for_load_state("networkidle")
    page.locator("#quick-add [data-qadd-due]").click()
    page.locator(".ctx-item:has-text('Завтра')").click()
    expect(page.locator("#quick-add [data-qadd-due] .qadd-due-lbl")).to_have_text("завтра")
    box = page.locator("#quick-add .qadd-input")
    box.fill("забрать пропуск")
    box.press("Enter")
    row = page.locator(".trow:has-text('забрать пропуск')")
    expect(row).to_be_visible()
    expect(row.locator(".trow-date")).to_have_text("завтра")


def test_the_add_row_stays_put_while_the_new_card_is_scrolled_to(page: Page):
    """Подсветка новой строки уводит длинный список вниз. Строка добавления липкая — иначе
    поле ввода уезжает с экрана и следующую задачу набирают вслепую."""
    page.goto("/view/commitments?account=")
    page.wait_for_load_state("networkidle")
    pos = page.evaluate(
        "() => getComputedStyle(document.getElementById('quick-add')).position"
    )
    assert pos == "sticky", pos
    # …и стоит ровно под полосой инструментов, а не поверх неё.
    gap = page.evaluate(
        "() => document.getElementById('quick-add').getBoundingClientRect().top"
        " - document.getElementById('board-tools').getBoundingClientRect().bottom"
    )
    assert abs(gap) < 2, gap


def test_a_task_that_landed_on_another_board_says_where_it_went(page: Page):
    """С «Жду ответа» задача уходит на «Мои задачи»: подсказка называет доску и открывает её."""
    page.goto("/view/waiting?account=")
    page.wait_for_load_state("networkidle")
    page.locator("[data-quick-add]").click()
    box = page.locator("#quick-add .qadd-input")
    box.fill("разобрать почту")
    box.press("Enter")
    toast = page.locator(".toast")
    expect(toast).to_contain_text("Мои задачи")
    expect(toast).to_contain_text("разобрать почту")
    toast.locator(".toast-undo").click()  # «Открыть»
    page.wait_for_load_state("networkidle")
    expect(page.locator("#list")).to_contain_text("разобрать почту")


def test_choosing_a_date_shows_a_real_date_field(page: Page):
    """«Выбрать дату…» раньше подставляло невидимый input и звало showPicker(): если он
    молчал, не происходило ничего. Теперь поле видно, и календарь открывает оно само."""
    page.goto("/view/commitments?account=")
    page.wait_for_load_state("networkidle")
    # Не «Сверить счёт с договором»: её снимает с доски тест истории выше по файлу.
    row = page.locator(".trow:has-text('Сверить доступы к API')")
    row.hover()
    row.locator("[data-due-btn]").click()
    page.locator(".ctx-item:has-text('Выбрать дату…')").click()
    field = page.locator(".ctx-date input[type=date]")
    expect(field).to_be_visible()
    field.fill("2026-12-31")
    page.locator(".ctx-date .btn:has-text('Готово')").click()
    page.wait_for_load_state("networkidle")
    expect(row.locator(".trow-date")).not_to_have_text("")


def test_a_card_carries_the_label_of_its_thread(page: Page):
    """Правило «Метки и правила» про адрес — это «от кого это». На письме чип был, на
    задаче из той же беседы — нет."""
    page.goto("/view/commitments?account=")
    page.wait_for_load_state("networkidle")
    row = page.locator(".trow:has-text('Сверить доступы к API')")
    expect(row).to_be_visible()
    expect(row.locator(".lbl-chip")).to_have_text("БЛ")


def test_the_reply_board_is_named_by_its_action(page: Page):
    page.goto("/view/needreply?account=")
    page.wait_for_load_state("networkidle")
    expect(page.locator(".main-head h1")).to_have_text("Ответить")
    expect(page.locator(".nav .nav-item:has-text('Ответить')")).to_have_count(1)


def test_a_wider_window_gives_the_list_its_width(page: Page):
    """Список упирался в 820px: окно шире — справа пусто, а заголовок обрублен."""
    page.goto("/view/commitments?account=")
    page.wait_for_load_state("networkidle")
    page.set_viewport_size({"width": 1100, "height": 800})
    narrow = page.locator("#list").bounding_box()["width"]
    page.set_viewport_size({"width": 1800, "height": 800})
    wide = page.locator("#list").bounding_box()["width"]
    assert wide > narrow + 500, (narrow, wide)
    # …и ширину списку задаёт область содержимого, а не константа в CSS.
    gap = page.evaluate(
        "() => {const c = document.querySelector('.content');"
        "const s = getComputedStyle(c);"
        "return c.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight)"
        " - document.getElementById('list').getBoundingClientRect().width;}"
    )
    assert abs(gap) < 2, gap


def test_closing_a_card_keeps_the_board_where_it_was(page: Page):
    """«Готово» в середине длинной доски: доска остаётся на месте, следующая карточка —
    на экране. Раньше забытый клавиатурный курсор вверху уводил доску наверх."""
    page.goto("/view/delegated?account=")
    content = page.locator(".content")
    page.keyboard.press("j")  # курсор на первой строке — как после любого j/k или добавления
    content.evaluate("c => c.scrollTop = 900")
    row = page.locator("#list .trow[data-item-id]:has-text('Поручение 20')")
    row.click()
    expect(page.locator("#pane [data-open-item]")).to_be_visible()
    top = content.evaluate("c => c.scrollTop")
    page.locator("#pane button[hx-post$='/complete']").click()
    expect(row).to_have_count(0)
    expect(page.locator("#list .trow.sel")).to_be_in_viewport()
    assert abs(content.evaluate("c => c.scrollTop") - top) < 100

    # Та же история с галочкой на самой строке.
    other = page.locator("#list .trow[data-item-id]:has-text('Поручение 25')")
    other.hover()  # Playwright сам докручивает строку в окно — меряем после этого
    top = content.evaluate("c => c.scrollTop")
    other.locator(".act.ok").click()
    expect(other).to_have_count(0)
    assert abs(content.evaluate("c => c.scrollTop") - top) < 100
