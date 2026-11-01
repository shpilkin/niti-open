"""Черновики писем: что сохраняется, что нет и что переживает отправку."""

from __future__ import annotations

import uuid

import pytest
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import func, select

from email_reader import maildrafts
from email_reader.models import Account, Draft
from email_reader.paths import resource_path
from tests.conftest import SPACE_ID, mk_space

_ENV = Environment(loader=FileSystemLoader(str(resource_path("templates"))), autoescape=True)
_ENV.globals["icon"] = lambda name, *a, **k: ""


async def _account(s, login: str = "me@ya.ru") -> Account:
    await mk_space(s)
    acc = Account(space_id=SPACE_ID, name=login, provider="imap", login=login,
                  host="imap.x", port=993, enabled=True)
    s.add(acc)
    await s.flush()
    return acc


def _fields(**over) -> dict:
    base = {"draft_id": "", "to": "", "cc": "", "subject": "", "body": "",
            "in_reply_to": "", "references": "", "forward_email_id": "", "back_url": ""}
    base.update(over)
    return base


async def _count(s) -> int:
    return int((await s.execute(select(func.count()).select_from(Draft))).scalar() or 0)


@pytest.mark.asyncio
async def test_an_empty_composer_leaves_no_draft(db):
    async with db() as s:
        acc = await _account(s)
        assert await maildrafts.save(s, account_id=acc.id, **_fields()) is None
        assert await _count(s) == 0


@pytest.mark.asyncio
async def test_the_second_save_edits_the_same_draft(db):
    async with db() as s:
        acc = await _account(s)
        first = await maildrafts.save(
            s, account_id=acc.id, **_fields(to="bob@x.ru", subject="Смета")
        )
        again = await maildrafts.save(
            s, account_id=acc.id,
            **_fields(draft_id=str(first.id), to="bob@x.ru", subject="Смета на 2027")
        )
        assert again.id == first.id
        assert again.subject == "Смета на 2027"
        assert await _count(s) == 1


@pytest.mark.asyncio
async def test_a_late_save_does_not_resurrect_a_sent_draft(db):
    """Автосохранение отложено на секунды и приходит уже после отправки — воскресить
    удалённый черновик оно не должно."""
    async with db() as s:
        acc = await _account(s)
        gone = uuid.uuid4()
        late = await maildrafts.save(
            s, account_id=acc.id, **_fields(draft_id=str(gone), to="bob@x.ru", body="текст")
        )
        assert late is None
        assert await _count(s) == 0


@pytest.mark.asyncio
async def test_discard_removes_only_the_named_draft(db):
    async with db() as s:
        acc = await _account(s)
        one = await maildrafts.save(s, account_id=acc.id, **_fields(subject="Раз"))
        await maildrafts.save(s, account_id=acc.id, **_fields(subject="Два"))
        assert await maildrafts.discard(s, str(one.id)) is True
        assert await maildrafts.discard(s, str(one.id)) is False
        assert [d.subject for d in await maildrafts.for_account(s, acc.id)] == ["Два"]


@pytest.mark.asyncio
async def test_drafts_are_counted_per_mailbox(db):
    async with db() as s:
        work = await _account(s, "work@x.ru")
        home = await _account(s, "home@ya.ru")
        await maildrafts.save(s, account_id=work.id, **_fields(subject="Раз"))
        await maildrafts.save(s, account_id=work.id, **_fields(subject="Два"))
        await maildrafts.save(s, account_id=home.id, **_fields(subject="Три"))
        counts = await maildrafts.counts_by_account(s)
        assert counts == {work.id: 2, home.id: 1}


def _render_compose(ctx: dict | None, sig_text: str) -> str:
    acc_id = uuid.UUID(int=7)
    accounts = [type("A", (), {"id": acc_id, "name": "Ящик", "login": "me@ya.ru"})()]
    return _ENV.get_template("_compose.html").render(
        accounts=accounts, ctx=ctx, signatures={}, sig_fonts={},
        sig_texts={str(acc_id): sig_text},
    )


def test_a_new_letter_gets_the_signature_and_no_draft_id():
    html = _render_compose(None, "Глеб\nPM")
    assert "-- \nГлеб\nPM" in html
    assert 'name="draft_id" value=""' in html


def test_a_reopened_draft_keeps_one_signature():
    """Тело черновика уже содержит подпись: подставить её второй раз — отправить письмо
    с двумя подписями."""
    body = "мой текст\n\n-- \nГлеб\nPM"
    html = _render_compose(
        {"to": "bob@x.ru", "cc": "", "subject": "Смета", "body": body, "draft": "",
         "quoted": "", "in_reply_to": None, "references": [],
         "forward_email_id": None, "account_id": uuid.UUID(int=7), "id": uuid.UUID(int=9)},
        "Глеб\nPM",
    )
    assert "мой текст" in html  # текст черновика, а не пустая форма с подписью
    assert html.count("-- \nГлеб\nPM") == 1
    assert f'name="draft_id" value="{uuid.UUID(int=9)}"' in html
    assert "Черновик" in html
