"""The search screens: the results page, its chips, and the ⌘K palette.

Rendering matters here for one reason beyond looks — a snippet is other people's mail,
and it reaches the page as HTML.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from html import unescape as html_unescape

import email_reader.api.app  # noqa: F401  — registers the filters on the template env
from email_reader.api.common import templates
from email_reader.api.routers.search import CHIPS
from email_reader.enums import ActionStatus, ActionType, Direction
from email_reader.models import Account, ActionItem, Email
from email_reader.search import index as ix
from email_reader.search import query as q
from tests.conftest import SPACE_ID, mk_space

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
LABELS = {k: v[0] for k, v in ix.KINDS.items()}


def _render(name: str, **ctx) -> str:
    return templates.env.get_template(name).render(KIND_LABEL=LABELS, **ctx)


async def _seed(s):
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@b.ru", folders=[])
    s.add(acc)
    await s.flush()
    mail = Email(account_id=acc.id, dedup_hash=uuid.uuid4().hex, direction=Direction.INBOUND,
                 from_addr="petrov@romashka.ru", from_name="Пётр Петров",
                 to_addrs=[{"email": "me@b.ru"}], cc_addrs=[], subject="Смета на тендер",
                 body_text="Прошу согласовать смету до пятницы.",
                 sent_at=NOW - timedelta(days=1))
    item = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY,
                      title="Ответить по смете", status=ActionStatus.OPEN,
                      counterparty="Пётр Петров")
    s.add_all([mail, item])
    await s.flush()
    await ix.index_mail(s, mail)
    await ix.index_card(s, item)
    await s.commit()
    return mail, item


async def test_results_are_grouped_by_kind_with_counts_and_links(db):
    async with db() as s:
        mail, item = await _seed(s)
        parsed, results = await q.search(s, "смета", space_id=SPACE_ID, today=TODAY)
        html = _render("_searchresults.html", q="смета", parsed=parsed,
                       groups=q.grouped(results), total=len(results), chips=CHIPS,
                       active_kinds=parsed.kinds, by_date=False)
    assert "Найдено: 2" in html
    assert "Письма" in html and "Обязательства" in html
    assert f'hx-get="/email/{mail.id}"' in html
    assert f'hx-get="/item/{item.id}"' in html
    assert "<mark>" in html, "the matched words must be marked in the snippet"


async def test_a_snippet_cannot_smuggle_html_out_of_a_letter(db):
    """The body is someone else's text; only the index's own markers become tags."""
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@b.ru", folders=[])
        s.add(acc)
        await s.flush()
        mail = Email(account_id=acc.id, dedup_hash=uuid.uuid4().hex,
                     direction=Direction.INBOUND, from_addr="x@y.ru", to_addrs=[],
                     cc_addrs=[], subject="Смета",
                     body_text="<script>alert('смета')</script> обычный текст",
                     sent_at=NOW)
        s.add(mail)
        await s.flush()
        await ix.index_mail(s, mail)
        await s.commit()
        parsed, results = await q.search(s, "смета", space_id=SPACE_ID, today=TODAY)
        html = _render("_searchresults.html", q="смета", parsed=parsed,
                       groups=q.grouped(results), total=len(results), chips=CHIPS,
                       active_kinds=[], by_date=False)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


async def test_a_chip_adds_and_removes_its_operator(db):
    async with db() as s:
        await _seed(s)
        parsed, results = await q.search(s, "смета где:письма", space_id=SPACE_ID,
                                         today=TODAY)
        html = _render("_searchresults.html", q="смета где:письма", parsed=parsed,
                       groups=q.grouped(results), total=len(results), chips=CHIPS,
                       active_kinds=parsed.kinds, by_date=False)
    assert parsed.kinds == ["mail"]
    assert {r.kind for r in results} == {"mail"}
    # hx-vals is JSON, so the queries the chips offer are read back out of it. The
    # active chip drops its own operator, every other chip adds one.
    offered = [json.loads(html_unescape(v))["q"]
               for v in re.findall(r"hx-vals='([^']+)'", html)]
    assert "смета" in offered
    assert any("где:обязательства" in o for o in offered)


async def test_the_palette_lists_a_few_of_each_kind_and_keeps_keyboard_hints(db):
    async with db() as s:
        await _seed(s)
        _, results = await q.search(s, "смет", space_id=SPACE_ID, per_kind=4,
                                    prefix=True, today=TODAY)
        html = _render("_palette_results.html", q="смет", groups=q.grouped(results),
                       total=len(results))
    assert results, "a half-typed word must already find something"
    assert "Enter" in html and "Esc" in html
    assert 'data-pane="1"' in html


async def test_an_empty_query_explains_the_operators(db):
    html = _render("_searchresults.html", q="", parsed=q.parse(""), groups=[], total=0,
                   chips=CHIPS, active_kinds=[], by_date=False)
    assert "от:петров" in html and "где:встречи" in html


async def test_a_letter_row_shows_the_hour_and_says_it_has_an_attachment(db):
    """В строке письма была одна дата: два письма одного дня выглядели одинаково, а про
    вложение строка молчала (docs/feature-search-mcp-agent.md §9)."""
    async with db() as s:
        mail, item = await _seed(s)
        mail.has_attachments = True
        await s.flush()
        await ix.index_mail(s, mail)
        await s.commit()
        parsed, results = await q.search(s, "смета", space_id=SPACE_ID, today=TODAY)
        html = _render("_searchresults.html", q="смета", parsed=parsed,
                       groups=q.grouped(results), total=len(results), chips=CHIPS,
                       active_kinds=parsed.kinds, by_date=False, capped=False)
    rows = {r for r in html.split("<li ") if "srow" in r}
    letter = next(r for r in rows if str(mail.id) in r)
    card = next(r for r in rows if str(item.id) in r)
    assert "05.09.2026 12:00" in letter, "у письма показываем час, а не только дату"
    assert 'title="Есть вложение"' in letter
    # У обязательства часа нет: там он ничего не уточняет.
    assert "12:00" not in card and 'title="Есть вложение"' not in card


async def test_the_count_does_not_pretend_the_page_is_everything(db):
    async with db() as s:
        await _seed(s)
        parsed, results = await q.search(s, "смета", space_id=SPACE_ID, today=TODAY)
        capped = _render("_searchresults.html", q="смета", parsed=parsed,
                         groups=q.grouped(results), total=len(results), chips=CHIPS,
                         active_kinds=parsed.kinds, by_date=False, capped=True)
        exact = _render("_searchresults.html", q="смета", parsed=parsed,
                        groups=q.grouped(results), total=len(results), chips=CHIPS,
                        active_kinds=parsed.kinds, by_date=False, capped=False)
    assert "Найдено: 2 и больше" in capped
    assert "Найдено: 2 по" in exact
