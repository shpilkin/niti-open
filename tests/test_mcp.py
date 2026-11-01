"""MCP: the token that guards the endpoint, and the tools behind it.

The tools are tested directly (no transport): they are the contract an outside agent
sees, and the transport is the SDK's business, not ours.
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from email_reader import compose, maildrafts, views
from email_reader import projects as projects_svc
from email_reader.enums import ActionStatus, ActionType, Direction, SourceKind
from email_reader.mcp import auth, tools
from email_reader.models import Account, ActionItem, Email, Meeting
from email_reader.search import index as ix
from tests.conftest import SPACE_ID, mk_space

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


async def _setup(s):
    work = await mk_space(s, 1, name="Работа")
    home = await mk_space(s, 2, name="Личное")
    wa = Account(space_id=work.id, name="w", host="h", login="me@work.ru", folders=[])
    ha = Account(space_id=home.id, name="h", host="h", login="me@home.ru", folders=[])
    s.add_all([wa, ha])
    await s.flush()
    work_mail = Email(account_id=wa.id, dedup_hash=uuid.uuid4().hex,
                      rfc_message_id="<smeta-1@romashka.ru>", reference_ids=["<nul@x>"],
                      direction=Direction.INBOUND, from_addr="petrov@romashka.ru",
                      from_name="Пётр Петров", to_addrs=[{"email": "me@work.ru"}],
                      cc_addrs=[], subject="Смета на тендер", body_text="Согласуйте смету.",
                      sent_at=NOW - timedelta(days=1))
    home_mail = Email(account_id=ha.id, dedup_hash=uuid.uuid4().hex,
                      direction=Direction.INBOUND, from_addr="shop@x.ru", to_addrs=[],
                      cc_addrs=[], subject="Смета на ремонт кухни", body_text="—",
                      sent_at=NOW)
    card = ActionItem(space_id=work.id, type=ActionType.NEED_MY_REPLY,
                      title="Ответить по смете", status=ActionStatus.OPEN,
                      counterparty="Пётр Петров")
    s.add_all([work_mail, home_mail, card])
    await s.flush()
    for obj, fn in ((work_mail, ix.index_mail), (home_mail, ix.index_mail),
                    (card, ix.index_card)):
        await fn(s, obj)
    await s.commit()
    return work, home, work_mail, home_mail, card


# --- the token -------------------------------------------------------------------------


async def test_the_token_is_stored_hashed_and_checked_as_a_bearer(db):
    async with db() as s:
        assert await auth.check(s, "Bearer anything") is False  # nothing issued yet
        token = await auth.issue(s)
        await s.commit()
        stored = await auth.has_token(s)
        assert stored and len(token) > 30
        assert await auth.check(s, f"Bearer {token}") is True
        assert await auth.check(s, f"bearer {token}") is True
        assert await auth.check(s, token) is False          # scheme required
        assert await auth.check(s, "Bearer nope") is False
        assert await auth.check(s, None) is False
        # The plain token is nowhere in the database.
        from email_reader import settings_store
        assert await settings_store.get_value(s, auth.TOKEN_KEY) != token


async def test_a_new_token_invalidates_the_old_one_and_revoke_closes_the_door(db):
    async with db() as s:
        first = await auth.issue(s)
        second = await auth.issue(s)
        await s.commit()
        assert await auth.check(s, f"Bearer {first}") is False
        assert await auth.check(s, f"Bearer {second}") is True
        await auth.revoke(s)
        await s.commit()
        assert await auth.check(s, f"Bearer {second}") is False


# --- tools -----------------------------------------------------------------------------


async def test_a_tool_refuses_an_unknown_space_instead_of_guessing(db):
    """`spaces.resolve` falls back to the default space; for a caller outside the app
    answering about the wrong mailbox is worse than an error."""
    async with db() as s:
        await _setup(s)
        with pytest.raises(tools.ToolError) as err:
            await tools.search(s, query="смета", space="Неведомое")
        assert "Работа" in str(err.value) and "Личное" in str(err.value)
        with pytest.raises(tools.ToolError):
            await tools.search(s, query="смета", space="")


async def test_search_stays_inside_the_space_it_was_given(db):
    async with db() as s:
        _, _, work_mail, home_mail, _ = await _setup(s)
        at_work = await tools.search(s, query="смета", space="Работа")
        at_home = await tools.search(s, query="смета", space="Личное")
    work_ids = {r["id"] for r in at_work["results"]}
    home_ids = {r["id"] for r in at_home["results"]}
    assert work_mail.id.hex in work_ids and home_mail.id.hex not in work_ids
    assert home_mail.id.hex in home_ids and work_mail.id.hex not in home_ids


async def test_letters_come_back_marked_as_other_peoples_text(db):
    async with db() as s:
        _, _, work_mail, _, _ = await _setup(s)
        mail = await tools.get_email(s, email_id=str(work_mail.id))
    assert mail["body"].startswith(tools.FENCE_HEAD)
    assert mail["body"].endswith(tools.FENCE_TAIL)
    assert "Согласуйте смету." in mail["body"]


async def test_a_card_can_be_created_from_one_line_and_then_closed(db):
    async with db() as s:
        await _setup(s)
        made = await tools.create_card(s, space="Работа", text="Позвонить Пете в пятницу")
        await s.commit()
        assert made["title"] == "Позвонить Пете"
        assert made["due"] is not None, "the date inside the line must be read out"

        closed = await tools.update_card(s, card_id=made["id"], action="done")
        await s.commit()
    assert closed["status"] == ActionStatus.DONE


async def test_update_card_rejects_an_action_it_does_not_know(db):
    async with db() as s:
        await _setup(s)
        made = await tools.create_card(s, space="Работа", text="Что-то сделать")
        await s.commit()
        with pytest.raises(tools.ToolError):
            await tools.update_card(s, card_id=made["id"], action="удалить навсегда")


def test_there_is_no_tool_that_sends_a_letter():
    """Отправку сняли целиком (§8.1): ни инструмента, ни двухшагового обхода."""
    from email_reader.mcp import server as mcp_server

    assert not hasattr(tools, "send_email")
    assert not hasattr(tools, "prepare_email")
    source = pathlib.Path(mcp_server.__file__).read_text()
    assert "send_email" not in source and "prepare_email" not in source


async def test_save_draft_lands_in_the_mailbox_drafts_and_sends_nothing(db, monkeypatch):
    sent: list[dict] = []

    async def fake_send(session, **kw):  # noqa: ANN001
        sent.append(kw)
        return compose.SendResult()

    monkeypatch.setattr("email_reader.compose.send", fake_send)
    async with db() as s:
        work, _home, work_mail, _hm, _card = await _setup(s)
        saved = await tools.save_draft(
            s, space="Работа", to="petrov@romashka.ru", subject="Смета",
            body="Отправляю смету.", reply_to_email_id=str(work_mail.id),
        )
        await s.commit()
        assert sent == [], "черновик не отправляет письмо"
        assert saved["box"] == "me@work.ru"
        assert saved["open"] == f"/compose?draft_id={saved['draft_id']}"

        # Он лежит именно там, где его ищет человек: в «Черновиках» своего ящика.
        acc = (await s.execute(select(Account).where(Account.login == "me@work.ru"))
               ).scalar_one()
        rows = await maildrafts.for_account(s, acc.id)
        assert [d.subject for d in rows] == ["Смета"]
        assert rows[0].to_line == "petrov@romashka.ru"
        # Ответ продолжает беседу, а не начинает новую.
        assert rows[0].in_reply_to == "<smeta-1@romashka.ru>"
        assert rows[0].references == "<nul@x> <smeta-1@romashka.ru>"
        assert rows[0].back_url == f"/email/{work_mail.id}"


async def test_save_draft_refuses_an_empty_letter(db):
    async with db() as s:
        await _setup(s)
        with pytest.raises(tools.ToolError):
            await tools.save_draft(s, space="Работа")


async def test_remember_writes_the_owners_memory_not_the_models(db):
    async with db() as s:
        await _setup(s)
        into_profile = await tools.remember(s, text="Счета шлю в бухгалтерию",
                                            space="Работа")
        await s.commit()
        from email_reader import profiles
        assert into_profile["stored"] == "profile"
        assert "бухгалтерию" in await profiles.get_profile(s, SPACE_ID)


async def test_get_context_hands_the_outside_agent_what_the_prompts_start_from(db):
    """The owner's profile, the projects and the standing rules — readable, and only
    for the space that was asked about."""
    async with db() as s:
        work, home, *_ = await _setup(s)
        from email_reader import profiles
        from email_reader import projects as projects_svc
        from email_reader.memory import rules as rules_svc
        await profiles.set_profile(s, work.id, "Роль: PM.\nКоманды:\n- Аналитика (Оля).")
        await profiles.set_profile(s, home.id, "Дача и ремонт.")
        await projects_svc.create_project(
            s, name="Тендер", prompt="Закупка для Ромашки, без поддержки.",
            space_id=work.id, aliases=["тендер-2026"],
        )
        await rules_svc.accept(s, match_type="domain", pattern="spam.ru", action="ignore",
                               value=None)
        await s.commit()
        ctx = await tools.get_context(s, space="Работа")
        empty = await tools.get_context(s, space="Личное")
    assert ctx["space"] == "Работа"
    assert "Аналитика" in ctx["profile"] and "Дача" not in ctx["profile"]
    assert [(p["name"], p["description"], p["aliases"]) for p in ctx["threads"]] == [
        ("Тендер", "Закупка для Ромашки, без поддержки.", ["тендер-2026"])
    ]
    assert ctx["rules"] == [{"pattern": "spam.ru", "match": "domain",
                             "rule": "не разбирать письма"}]
    assert "Дача" in empty["profile"] and empty["threads"] == []


async def test_meetings_and_calendar_are_scoped_too(db):
    async with db() as s:
        work, home, *_ = await _setup(s)
        s.add(Meeting(ext_id="m1", space_id=work.id, started_at=NOW, title="Планёрка"))
        await s.commit()
        here = await tools.list_meetings(s, space="Работа")
        there = await tools.list_meetings(s, space="Личное")
    assert [m["title"] for m in here["meetings"]] == ["Планёрка"]
    assert there["meetings"] == []


async def test_list_cards_names_the_boards_when_asked_for_a_wrong_one(db):
    async with db() as s:
        await _setup(s)
        with pytest.raises(tools.ToolError) as err:
            await tools.list_cards(s, space="Работа", board="везде")
    assert "today" in str(err.value)


def test_every_registered_tool_has_a_russian_description():
    """The description is what the outside agent reads to choose a tool."""
    server = __import__("email_reader.mcp.server", fromlist=["server"]).build_server()
    import asyncio
    listed = asyncio.run(server.list_tools())
    assert len(listed) >= 22
    assert {tool.name for tool in listed} >= {"list_spaces", "get_context", "search"}
    for tool in listed:
        assert tool.description and len(tool.description) > 20, tool.name
        assert any("а" <= ch <= "я" for ch in tool.description.lower()), tool.name


def test_a_prefilled_compose_link_actually_fills_the_form():
    """The tool promises a form with the fields already in it. `/compose` builds that
    context out of its query string; this is the form it renders from it."""
    import email_reader.api.app  # noqa: F401 — registers icon()/filters
    from email_reader.api.common import templates

    link = tools.compose_link(to="petrov@romashka.ru", subject="Смета",
                              body="Отправляю смету.")
    assert link["open"] == ("/compose?to=petrov%40romashka.ru&subject=%D0%A1%D0%BC%D0%B5"
                            "%D1%82%D0%B0&body=%D0%9E%D1%82%D0%BF%D1%80%D0%B0%D0%B2%D0"
                            "%BB%D1%8F%D1%8E+%D1%81%D0%BC%D0%B5%D1%82%D1%83.")
    ctx = {"to": "petrov@romashka.ru", "cc": "", "subject": "Смета",
           "draft": "Отправляю смету.", "quoted": "", "in_reply_to": None,
           "references": [], "forward_email_id": None, "account_id": None}
    html = templates.env.get_template("_compose.html").render(
        accounts=[type("A", (), {"id": uuid.uuid4(), "login": "me@work.ru"})()],
        ctx=ctx, signatures={}, sig_texts={},
    )
    assert 'value="petrov@romashka.ru"' in html
    assert 'value="Смета"' in html
    assert "Отправляю смету." in html


async def test_a_person_known_only_by_an_identity_does_not_match_every_card(db):
    """A profile can carry neither a name nor an address — the name lives on an identity
    row instead. Matching open cards on an empty string would return the whole space."""
    from email_reader.memory import people as people_svc
    from email_reader.models import Person

    async with db() as s:
        work, *_ = await _setup(s)
        s.add(ActionItem(space_id=work.id, type=ActionType.NEED_MY_REPLY, title="Чужая",
                         status=ActionStatus.OPEN, counterparty="Кто-то другой"))
        bare = Person(name=None, email=None)
        s.add(bare)
        await s.flush()
        # Identities are keyed by the normalised name (people.name_key), the display
        # form is kept beside it.
        await people_svc.add_identity(s, bare, "name", people_svc.name_key("Тихий Человек"),
                                      "Тихий Человек")
        await s.commit()
        found = await tools.get_person(s, who="Тихий Человек", space="Работа")
    assert found["open_cards"] == []


async def test_handing_a_card_over_from_outside_lands_in_the_journal(db):
    from sqlalchemy import select as sa_select

    from email_reader.models import AgentDecision

    async with db() as s:
        await _setup(s)
        made = await tools.create_card(s, space="Работа", text="Оценить API")
        await tools.update_card(s, card_id=made["id"], delegate_to="Борис")
        await s.commit()
        rows = (await s.execute(sa_select(AgentDecision).where(
            AgentDecision.action_item_id == uuid.UUID(made["id"])
        ))).scalars().all()
    delegations = [d for d in rows if d.payload.get("action") == "delegate"]
    assert len(delegations) == 1
    assert delegations[0].payload["to"] == "Борис"
    assert delegations[0].payload["via"] == "mcp"


async def test_get_context_about_brings_the_memory_the_prompts_get(db):
    """`about` names a person or a topic: the answer carries that person's card, the
    earlier threads with them and the facts that touch the words — from the builder the
    extractor uses, so the agent outside and the model inside see the same memory."""
    from email_reader import profiles
    from email_reader.memory import people as people_svc
    from email_reader.models import Thread

    async with db() as s:
        work, _home, work_mail, *_ = await _setup(s)
        await profiles.set_profile(s, work.id, "Роль: PM.")
        await profiles.remember_line(s, work.id, "смету на тендер согласую с Катей")
        petrov = await people_svc.ensure_person(s, "petrov@romashka.ru", "Пётр Петров")
        await people_svc.set_notes(s, petrov, "CTO Ромашки")
        # An earlier thread with them — precedents are threads, and _setup's mail has none.
        thread = Thread(account_id=work_mail.account_id)
        s.add(thread)
        await s.flush()
        s.add(Email(account_id=work_mail.account_id, thread_id=thread.id,
                    dedup_hash=uuid.uuid4().hex, direction=Direction.INBOUND,
                    from_addr="petrov@romashka.ru", to_addrs=[{"email": "me@work.ru"}],
                    cc_addrs=[], subject="Смета на тендер: сроки",
                    sent_at=NOW - timedelta(days=3)))
        await s.commit()
        plain = await tools.get_context(s, space="Работа")
        person = await tools.get_context(s, space="Работа", about="petrov@romashka.ru")
        topic = await tools.get_context(s, space="Работа", about="Смета на тендер")
    assert plain["about"] is None and "Катей" in plain["profile"]
    assert person["about"]["people"][0]["name"] == "Пётр Петров"
    assert person["about"]["people"][0]["notes"] == "CTO Ромашки"
    assert [p["subject"] for p in person["about"]["precedents"]] == ["Смета на тендер: сроки"]
    assert [f["text"] for f in topic["about"]["facts"]] == [
        f"{datetime.now(UTC):%Y-%m-%d}: смету на тендер согласую с Катей"
    ]
    assert topic["about"]["people"] == []


async def test_recall_hands_out_only_what_the_owner_has_said_yes_to(db):
    """`recall` is the facts section of the memory block, exposed: my remembered lines of
    the space and confirmed facts about people, by any of the words. A proposal capture
    wrote, a rejected one, another space's line — never (§6)."""
    from email_reader import profiles
    from email_reader.memory import capture, facts
    from email_reader.memory import people as people_svc

    async with db() as s:
        work, home, *_ = await _setup(s)
        await profiles.set_profile(s, work.id, "Роль: PM.")
        mine = await profiles.remember_line(s, work.id, "смету на тендер согласую с Катей", now=NOW)
        await profiles.remember_line(s, home.id, "смету на ремонт кухни держу у себя", now=NOW)
        kw = dict(subject_kind="me", kind="rule", evidence_kind="mail", evidence_id="a" * 32,
                  quote="q" * 12, captured_in=work.id, space_id=work.id, now=NOW)
        _, proposed = await facts.propose(s, text="смету на тендер показываю юристам", **kw)
        _, rejected = await facts.propose(s, text="смету на тендер отдаю Пете", **kw)
        await facts.reject(s, rejected, now=NOW)
        petrov = await people_svc.ensure_person(s, "petrov@romashka.ru", "Пётр Петров")
        _, about = await facts.propose(
            s, subject_kind="person", subject_key=petrov.id.hex, kind="role",
            text="Пётр — CTO Ромашки, тендеры его", evidence_kind="mail", evidence_id="b" * 32,
            quote="q" * 12, captured_in=home.id, now=NOW,
        )
        await capture.confirm(s, about, now=NOW)
        await s.commit()

        by_words = await tools.recall(s, space="Работа", query="тендер смета")
        everything = await tools.recall(s, space="Работа")
        elsewhere = await tools.recall(s, space="Личное", query="тендер")
    assert by_words["space"] == "Работа" and by_words["query"] == "тендер смета"
    assert [(f["text"], f["about"], f["kind"], f["matched"]) for f in by_words["facts"]] == [
        ("2026-09-06: смету на тендер согласую с Катей", None, "fact", True),
        ("2026-09-06: Пётр — CTO Ромашки, тендеры его", "Пётр Петров", "role", True),
    ]
    assert proposed.status == "proposed" and rejected.status == "rejected"
    # no words: my own lines of the space, nothing about people
    assert [f["text"] for f in everything["facts"]] == [
        "2026-09-06: смету на тендер согласую с Катей"
    ]
    assert everything["facts"][0]["matched"] is False
    # the other space sees its own line and the global fact about Пётр, not mine
    assert [f["text"] for f in elsewhere["facts"]] == [
        "2026-09-06: Пётр — CTO Ромашки, тендеры его",
        "2026-09-06: смету на ремонт кухни держу у себя",
    ]
    assert mine.id != proposed.id


# --- тема, разошедшаяся по перепискам --------------------------------------------------


async def _thread_with(s, account, subject: str, n: int, *, who: str, day: int):
    """Беседа из n писем — то, из чего складывается «тема»."""
    from email_reader.models import Thread

    th = Thread(account_id=account.id, subject_norm=subject.casefold(),
                first_at=NOW - timedelta(days=day), last_at=NOW - timedelta(days=day),
                message_count=n)
    s.add(th)
    await s.flush()
    mails = []
    for i in range(n):
        m = Email(account_id=account.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
                  direction=Direction.INBOUND, from_addr=f"{who}@romashka.ru",
                  from_name=who, to_addrs=[{"email": "me@work.ru"}], cc_addrs=[],
                  subject=subject, body_text=f"Тендер, письмо {i}. " + "х" * 900,
                  sent_at=NOW - timedelta(days=day, hours=n - i))
        s.add(m)
        mails.append(m)
    await s.flush()
    for m in mails:
        await ix.index_mail(s, m)
    th.last_at = mails[-1].sent_at
    await s.flush()
    return th, mails


async def test_list_conversations_maps_the_topic_one_row_per_conversation(db):
    async with db() as s:
        await _setup(s)
        acc = (await s.execute(select(Account).where(Account.login == "me@work.ru"))
               ).scalar_one()
        await _thread_with(s, acc, "Тендер на смету", 4, who="petrov", day=2)
        await _thread_with(s, acc, "Тендер: сроки", 2, who="ivanov", day=1)
        await s.commit()

        out = await tools.list_conversations(s, space="Работа", query="тендер")
        assert [r["messages"] for r in out["conversations"]] == [4, 2] or \
               [r["messages"] for r in out["conversations"]] == [2, 4]
        row = out["conversations"][0]
        assert row["participants"] and row["matched"] >= 1
        assert row["conversation_id"] and row["last_email_id"]
        assert out["more"] is False and out["cursor"] is None

        # Чужое пространство не просачивается: та же тема в «Личном» — не эта карта.
        home = (await s.execute(select(Account).where(Account.login == "me@home.ru"))
                ).scalar_one()
        await _thread_with(s, home, "Тендер на кухню", 3, who="shop", day=1)
        await s.commit()
        again = await tools.list_conversations(s, space="Работа", query="тендер")
        assert all("кухн" not in (r["subject"] or "").casefold()
                   for r in again["conversations"])
        assert all(r["messages"] != 3 or "кухн" not in str(r["participants"])
                   for r in again["conversations"])
        personal = await tools.list_conversations(s, space="Личное", query="тендер")
        assert [r["subject"] for r in personal["conversations"]] == ["Тендер на кухню"]

        # Страница на одну беседу — и продолжение видно.
        first = await tools.list_conversations(s, space="Работа", query="тендер", limit=1)
        assert len(first["conversations"]) == 1 and first["more"] is True
        second = await tools.list_conversations(s, space="Работа", query="тендер", limit=1,
                                          cursor=first["cursor"])
        assert len(second["conversations"]) == 1 and second["more"] is False
        assert (second["conversations"][0]["conversation_id"]
                != first["conversations"][0]["conversation_id"])


async def test_search_says_when_it_is_showing_only_a_page(db):
    async with db() as s:
        await _setup(s)
        acc = (await s.execute(select(Account).where(Account.login == "me@work.ru"))
               ).scalar_one()
        await _thread_with(s, acc, "Тендер на смету", 5, who="petrov", day=2)
        await s.commit()

        page = await tools.search(s, query="тендер", space="Работа", limit=2)
        assert page["found"] == 2 and page["more"] is True and page["cursor"] == 2
        rest = await tools.search(s, query="тендер", space="Работа", limit=10,
                                  cursor=page["cursor"])
        assert rest["more"] is False and rest["cursor"] is None
        seen = {r["id"] for r in page["results"]} | {r["id"] for r in rest["results"]}
        # Пять писем беседы плюс письмо «Смета на тендер» из общей заготовки.
        assert len(seen) == 6


async def test_get_conversation_gives_beginnings_by_default_and_texts_on_demand(db):
    async with db() as s:
        await _setup(s)
        acc = (await s.execute(select(Account).where(Account.login == "me@work.ru"))
               ).scalar_one()
        th, mails = await _thread_with(s, acc, "Тендер на смету", 3, who="petrov", day=1)
        await s.commit()

        digest = await tools.get_conversation(s, conversation_id=str(th.id))
        assert digest["mode"] == "digest" and digest["shown"] == 3
        assert digest["messages"][0]["from"] == "petrov"
        assert "note" in digest
        short = digest["messages"][0]["body"]
        assert "…" in short and len(short) < len(mails[0].body_text)

        full = await tools.get_conversation(s, conversation_id=str(th.id), mode="full")
        assert mails[0].body_text in full["messages"][0]["body"]

        # Хвост по дате: письма старше границы не приходят.
        since = (NOW - timedelta(days=1)).date().isoformat()
        assert await tools.get_conversation(s, conversation_id=str(th.id), since=since)


# --- карточки --------------------------------------------------------------------------


async def test_list_cards_slices_by_source_and_counterparty(db):
    async with db() as s:
        work, _home, _wm, _hm, _card = await _setup(s)
        from_tg = await views.create_task(
            s, title="Позвонить в банк", space_id=work.id, due=None,
            type_=ActionType.TASK, source_kind=SourceKind.TELEGRAM,
            source_ref="42:7", confirmed=False)
        await s.commit()

        tg = await tools.list_cards(s, space="Работа", source="telegram")
        assert [c["id"] for c in tg["cards"]] == [str(from_tg.id)]
        assert tg["filter"]["source"] == "telegram"

        by_person = await tools.list_cards(s, space="Работа", counterparty="Петров")
        assert [c["title"] for c in by_person["cards"]] == ["Ответить по смете"]

        with pytest.raises(tools.ToolError):
            await tools.list_cards(s, space="Работа", status="какой-то")


async def test_a_card_closed_from_outside_can_be_put_back_and_the_journal_says_who(db):
    async with db() as s:
        work, *_ = await _setup(s)
        made = await tools.create_card(s, space="Работа", text="Собрать смету")
        await tools.update_card(s, card_id=made["id"], action="done")
        await s.commit()

        back = await tools.reopen_card(s, card_id=made["id"])
        assert back["status"] == ActionStatus.OPEN
        await s.commit()

        # Второй раз возвращать нечего — карточка уже на доске.
        with pytest.raises(tools.ToolError):
            await tools.reopen_card(s, card_id=made["id"])

        from email_reader import history
        said = [e.text for e in await history.for_item(s, uuid.UUID(made["id"]))]
        assert "Готово, вручную (через MCP)" in said
        assert "Снова открыта, вручную (через MCP)" in said


async def test_the_same_action_from_the_app_is_not_marked_as_external(db):
    async with db() as s:
        work, *_ = await _setup(s)
        item = await views.create_task(s, title="Своя задача", space_id=work.id,
                                       due=None, type_=ActionType.TASK)
        await views.complete(s, item.id)
        await s.commit()
        from email_reader import history
        said = [e.text for e in await history.for_item(s, item.id)]
        assert "Готово, вручную" in said and "Готово, вручную (через MCP)" not in said


async def test_bulk_closes_many_cards_at_once(db):
    async with db() as s:
        await _setup(s)
        ids = [(await tools.create_card(s, space="Работа", text=f"Задача {i}"))["id"]
               for i in range(3)]
        await s.commit()
        out = await tools.bulk_update_cards(s, card_ids=ids, action="done")
        assert out == {"asked": 3, "changed": 3, "action": "complete",
                       "note": out["note"]}
        await s.commit()
        left = await tools.list_cards(s, space="Работа", status="живые", query="Задача")
        assert left["cards"] == []
        with pytest.raises(tools.ToolError):
            await tools.bulk_update_cards(s, card_ids=ids, action="сжечь")


async def test_get_card_hands_over_the_history_and_the_advice_already_computed(db):
    async with db() as s:
        work, _home, work_mail, _hm, card = await _setup(s)
        card.source_thread_id = None
        made = await tools.create_card(s, space="Работа", text="Собрать смету")
        await s.commit()

        out = await tools.get_card(s, card_id=made["id"])
        assert out["history"][0]["what"] == "Создана (через MCP)"
        # Совета ещё нет — и это сказано состоянием, а не пустотой.
        assert out["advice"] is None and "advice_state" in out


# --- нити ---------------------------------------------------------------------------


async def test_a_thread_gathers_its_conversations_meetings_and_open_cards(db):
    async with db() as s:
        work, _home, work_mail, _hm, card = await _setup(s)
        from email_reader.models import Project, Thread

        pr = Project(space_id=work.id, name="Тендер", aliases=["тендер-2026"])
        th = Thread(account_id=work_mail.account_id, subject_norm="смета",
                    first_at=NOW, last_at=NOW, message_count=1)
        s.add_all([pr, th])
        await s.flush()
        work_mail.thread_id = th.id
        card.source_thread_id = th.id
        await s.commit()

        attached = await tools.assign_to_thread(s, space="Работа", thread="тендер-2026",
                                                conversation_id=str(th.id))
        assert attached == {"thread": "Тендер", "attached": "conversation", "id": str(th.id)}
        await s.commit()

        out = await tools.get_thread(s, space="Работа", name="Тендер")
        assert [t["conversation_id"] for t in out["conversations"]] == [str(th.id)]
        assert [c["title"] for c in out["open_cards"]] == ["Ответить по смете"]

        with pytest.raises(tools.ToolError):
            await tools.get_thread(s, space="Работа", name="Которого нет")
        with pytest.raises(tools.ToolError):
            await tools.assign_to_thread(s, space="Работа", thread="Тендер")


# --- встречи ---------------------------------------------------------------------------


async def test_a_meeting_gives_the_moment_asked_for_not_the_whole_transcript(db, tmp_path):
    async with db() as s:
        work, *_ = await _setup(s)
        path = tmp_path / "talk.txt"
        # Расшифровка длиннее окна: иначе «кусок вокруг момента» и «всё целиком» — одно
        # и то же, и проверка ничего не проверяет.
        filler = "и так далее. " * 300
        path.write_text(f"[00:00:01] Начали. {filler}\n"
                        f"[00:12:30] Про смету: считаем до пятницы. {filler}\n"
                        f"[00:20:00] Разное, про отпуска. {filler}\n")
        meeting = Meeting(ext_id=f"m-{uuid.uuid4().hex}", space_id=work.id,
                          title="Планёрка", started_at=NOW,
                          transcript_path=str(path), participants=["Пётр Петров"],
                          summary="Обсудили смету")
        s.add(meeting)
        await s.commit()

        around = await tools.get_meeting(s, meeting_id=str(meeting.id), part="turns",
                                         around="[00:12:30]")
        assert "Про смету" in around["turns"]
        assert "Разное, про отпуска" not in around["turns"]
        assert "Начали" not in around["turns"]
        with pytest.raises(tools.ToolError):
            await tools.get_meeting(s, meeting_id=str(meeting.id), part="turns")
        with pytest.raises(tools.ToolError):
            await tools.get_meeting(s, meeting_id=str(meeting.id), part="turns",
                                    around="[09:99:99]")

        by_person = await tools.list_meetings(s, space="Работа", participant="Петров")
        assert [m["title"] for m in by_person["meetings"]] == ["Планёрка"]
        assert await tools.list_meetings(s, space="Работа",
                                         participant="Сидоров") == {
            "space": "Работа", "meetings": []}


def test_the_security_page_hands_out_every_client_shape_not_only_claude_code():
    """Подключаются разные клиенты: у одного команда в терминале, у другого файл
    настроек, у третьего просто адрес и заголовок. Плюс всё копируется кнопкой."""
    import email_reader.api.app  # noqa: F401 — регистрирует icon() и фильтры
    from email_reader.api.common import templates

    html = templates.env.get_template("settings/security.html").render(
        mcp_enabled=True, mcp_has_token=True, mcp_token="TOKEN123",
        mcp_url="http://127.0.0.1:8765/mcp")

    assert "claude mcp add --transport http niti" in html
    assert "mcpServers" in html and "&#34;type&#34;: &#34;http&#34;" in html
    assert "Authorization: Bearer TOKEN123" in html
    # Пять кнопок копирования: адрес, токен, команда, конфигурация, заголовок.
    assert html.count("data-copy=") == 5
    assert "Письма агент не отправляет" in html
    assert "в два шага" not in html


def test_nothing_in_the_settings_ui_draws_a_cross_with_a_glyph():
    """Иконки — только через icon(): юникодный крестик рисуется другим шрифтом и в
    разных темах выглядит по-разному (CLAUDE.md, DESIGN.md)."""
    for name in ("_labels.html", "_identities.html", "settings/security.html",
                 "settings/signatures.html"):
        text = pathlib.Path("templates", name).read_text()
        assert "✕" not in text and "×" not in text, name


async def test_a_new_card_says_which_board_it_landed_on(db):
    """Агент, создавший задачу, назвал доску наугад — и отправил человека искать её в
    «Сегодня», хотя задача со сроком на послезавтра лежит в «Моих задачах»."""
    async with db() as s:
        await _setup(s)
        when = (NOW + timedelta(days=2)).strftime("%d.%m")
        later = await tools.create_card(
            s, space="Работа", text=f"Собрать смету до {when}")
        today = await tools.create_card(s, space="Работа", text="Позвонить в банк сегодня")
        await s.commit()

        names = {b["key"] for b in later["boards"]}
        assert "commitments" in names and "today" not in names, later["boards"]
        assert "today" in {b["key"] for b in today["boards"]}


async def test_a_card_type_that_is_not_a_type_is_refused_not_stored(db):
    """Инструмент принимал «task», а в базе живёт «TASK»: карточка сохранялась с типом,
    которого нет, и пропадала со всех досок — её было видно только поиском."""
    async with db() as s:
        await _setup(s)
        lower = await tools.create_card(s, space="Работа", text="Позвонить в банк",
                                        type_="task")
        await s.commit()
        assert lower["type"] == ActionType.TASK
        assert [b["key"] for b in lower["boards"]], "карточка должна быть хоть на одной доске"

        with pytest.raises(tools.ToolError):
            await tools.create_card(s, space="Работа", text="Что-то", type_="важное")
        with pytest.raises(tools.ToolError):
            await tools.update_card(s, card_id=lower["id"], type_="важное")


# --- нити: предложения, привязки агента, сигналы, гайд --------------------------------


async def test_an_agent_proposes_a_thread_and_the_owner_decides(db):
    """Агент не создаёт живую нить: только предложение с уликами; «Да» владельца делает
    её нитью и привязывает улики как `agent`, «Нет» снимает и запоминает улики."""
    from email_reader.models import ProjectThread, Thread

    async with db() as s:
        work, _home, work_mail, _hm, card = await _setup(s)
        th = Thread(account_id=work_mail.account_id, subject_norm="видео",
                    first_at=NOW, last_at=NOW, message_count=1)
        s.add(th)
        await s.flush()
        work_mail.thread_id = th.id
        card.source_thread_id = th.id
        await s.commit()

        with pytest.raises(tools.ToolError):
            await tools.propose_thread(s, space="Работа", title="Видео", why="одна тема",
                                       evidence=["не id"])
        made = await tools.propose_thread(
            s, space="Работа", title="Видео", why="ByteDance и пилот — одно",
            evidence=[str(th.id), str(card.id)], by="Claude Code",
        )
        await s.commit()
        assert made["status"] == "proposed" and len(made["evidence"]) == 2
        listed = await tools.list_threads(s, space="Работа")
        assert [(t["name"], t["status"]) for t in listed["threads"]] == [("Видео", "proposed")]
        assert await projects_svc.list_projects(s, work.id) == []  # не в списках приложения
        with pytest.raises(tools.ToolError):  # к неподтверждённой нити привязывать нельзя
            await tools.assign_to_thread(s, space="Работа", thread="Видео",
                                         conversation_id=str(th.id))

        project = await projects_svc.get_project(s, uuid.UUID(made["id"]))
        await projects_svc.accept(s, project)
        await s.commit()
        got = await tools.get_thread(s, space="Работа", name="Видео")
        link = (await s.execute(
            select(ProjectThread).where(ProjectThread.project_id == project.id)
        )).scalar_one()
        assert got["status"] == "active" and got["state"] == "on_me" and got["on_me"] == 1
        assert [c["conversation_id"] for c in got["conversations"]] == [str(th.id)]
        assert link.source == "agent" and card.project_id == project.id

        other = await tools.propose_thread(s, space="Работа", title="Мусор", why="нет",
                                           evidence=[str(th.id)])
        await s.commit()
        await projects_svc.reject(s, await projects_svc.get_project(s, uuid.UUID(other["id"])))
        await s.commit()
        assert await projects_svc.get_project(s, uuid.UUID(other["id"])) is None
        assert str(th.id) in await projects_svc.rejected_sources(s)


async def test_agent_links_yield_to_the_owner_and_survive_the_classifier(db):
    from email_reader.models import Project, ProjectThread, Thread

    async with db() as s:
        work, _home, work_mail, _hm, card = await _setup(s)
        pr = Project(space_id=work.id, name="Тендер")
        th = Thread(account_id=work_mail.account_id, subject_norm="смета",
                    first_at=NOW, last_at=NOW, message_count=1)
        s.add_all([pr, th])
        await s.flush()
        card.source_thread_id = None
        await s.commit()

        out = await tools.assign_to_thread(s, space="Работа", thread="Тендер",
                                           conversation_id=str(th.id))
        assert out["attached"] == "conversation"
        row = (await s.execute(select(ProjectThread).where(ProjectThread.thread_id == th.id))
               ).scalar_one()
        assert row.source == "agent"
        # Авторазбор не перебивает и не снимает привязку агента.
        await projects_svc.assign(s, pr.id, th.id, source="auto")
        await projects_svc._remove_stale_auto(s, th.id, keep=set())
        await s.refresh(row)
        assert row.source == "agent"
        # Владелец перебивает агента, и агент уже не снимет.
        await projects_svc.assign(s, pr.id, th.id, source="manual")
        await s.commit()
        with pytest.raises(tools.ToolError):
            await tools.unassign_from_thread(s, space="Работа", thread="Тендер",
                                             conversation_id=str(th.id))
        # Карточка — прямой привязкой; снять её агент может.
        card_out = await tools.assign_to_thread(s, space="Работа", thread="Тендер",
                                                card_id=str(card.id))
        assert card_out["attached"] == "card" and card.project_id == pr.id
        await tools.unassign_from_thread(s, space="Работа", thread="Тендер",
                                         card_id=str(card.id))
        assert card.project_id is None
        # update_card(thread=) — та же прямая привязка.
        await tools.update_card(s, card_id=str(card.id), thread="Тендер")
        assert card.project_id == pr.id


async def test_update_thread_writes_three_fields_and_the_note_only(db):
    from email_reader.models import Project

    async with db() as s:
        work, *_ = await _setup(s)
        s.add_all([Project(space_id=work.id, name="Тендер"),
                   Project(space_id=work.id, name="Спрос")])
        await s.commit()
        with pytest.raises(tools.ToolError):  # имя занято
            await tools.update_thread(s, space="Работа", thread="Тендер", title="Спрос")
        out = await tools.update_thread(
            s, space="Работа", thread="Тендер", title="Тендер-2026",
            context="Закупка для Ромашки", aliases=["ромашка"], agent_note="проверить срок",
        )
        await s.commit()
        got = await tools.get_thread(s, space="Работа", name="ромашка")
    assert out["changed"] == ["title", "context", "aliases", "agent_note"]
    assert (got["name"], got["description"], got["agent_note"]) == (
        "Тендер-2026", "Закупка для Ромашки", "проверить срок")
    assert got["state"] == "quiet" and "stalled_days" not in got


async def test_signals_are_meetings_and_conversations_with_open_work_outside_threads(db):
    from email_reader.models import Meeting, Thread

    async with db() as s:
        work, _home, work_mail, _hm, card = await _setup(s)
        m = Meeting(ext_id="m-sig", space_id=work.id, started_at=NOW, title="Планёрка",
                    participants=["Борис", "Оля"])
        lonely = Meeting(ext_id="m-one", space_id=work.id, started_at=NOW, title="Один")
        th = Thread(account_id=work_mail.account_id, subject_norm="договор",
                    first_at=NOW, last_at=NOW, message_count=1)
        s.add_all([m, lonely, th])
        await s.flush()
        work_mail.thread_id = th.id
        card.source_thread_id = th.id
        for title, mid in (("Раз", m.id), ("Два", m.id), ("Три", lonely.id)):
            s.add(ActionItem(space_id=work.id, type=ActionType.TASK, title=title,
                             status=ActionStatus.OPEN, source_meeting_id=mid))
        s.add(ActionItem(space_id=work.id, type=ActionType.MY_COMMITMENT, title="Ещё",
                         status=ActionStatus.OPEN, source_thread_id=th.id))
        await s.commit()
        out = await tools.list_thread_signals(s, space="Работа")
        assert [(r["kind"], r["title"], r["open_cards"]) for r in out["signals"]] == [
            ("meeting", "Планёрка", 2), ("conversation", "Смета на тендер", 2),
        ]
        # После «Нет» с этой встречей в уликах сигнал молчит.
        pr = await projects_svc.propose(s, space_id=work.id, title="X", evidence=[
            {"kind": "meeting", "id": str(m.id), "title": "Планёрка"}], why="w")
        await projects_svc.reject(s, pr)
        await s.commit()
        again = await tools.list_thread_signals(s, space="Работа")
    assert [r["kind"] for r in again["signals"]] == ["conversation"]


async def test_the_journal_names_the_client_that_acted(db):
    from email_reader import history, views

    async with db() as s:
        work, _home, _wm, _hm, card = await _setup(s)
        token = views.ORIGIN.set("mcp/Claude Code")
        try:
            await tools.update_card(s, card_id=str(card.id), action="done")
        finally:
            views.ORIGIN.reset(token)
        await s.commit()
        story = [e.text for e in await history.for_item(s, card.id)]
    assert story[-1].endswith("(через MCP, Claude Code)")


def test_the_guide_is_served_three_ways_and_stays_short():
    server = __import__("email_reader.mcp.server", fromlist=["server"]).build_server()
    import asyncio

    tool_names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"get_guide", "propose_thread", "update_thread", "unassign_from_thread",
            "list_thread_signals"} <= tool_names
    resources = asyncio.run(server.list_resources())
    assert any(str(r.uri) == "niti://guide" for r in resources)
    prompts = {p.name for p in asyncio.run(server.list_prompts())}
    assert {"find_threads", "review_thread", "where_am_i_needed"} <= prompts
    guide = tools.get_guide()["guide"]
    assert "propose_thread" in guide and "Беседа" in guide and len(guide) < 9000


def test_the_skill_file_is_the_same_guide():
    """Третий носитель гайда — файл навыка в репозитории; расходиться с `guide.py` он
    не должен, иначе у агента будет две правды."""
    from pathlib import Path

    from email_reader.mcp.guide import GUIDE

    skill = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "niti-agent" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\nname: niti-agent\n")
    assert text.endswith(GUIDE)


def test_the_client_name_is_read_from_the_handshake():
    from types import SimpleNamespace

    from email_reader.mcp.server import _client_of

    new_style = SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(
        client_info=SimpleNamespace(name="Claude Code"))))
    old_style = SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(
        clientInfo=SimpleNamespace(name="goose"))))
    nameless = SimpleNamespace(session=SimpleNamespace(client_params=None))
    assert _client_of(new_style) == "Claude Code"
    assert _client_of(old_style) == "goose"
    assert _client_of(nameless) == "" and _client_of(object()) == ""
