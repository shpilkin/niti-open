"""The daily briefing: deterministic assembly, one structured call, a page — with a
deterministic fallback and a per-day cache."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

import email_reader.api.app  # noqa: F401  — registers icon/age/local on the template env
from email_reader.ai.extractor import Usage
from email_reader.ai.schemas import BriefingResult
from email_reader.api.common import templates
from email_reader.attention import ThreadAttention
from email_reader.enums import ActionStatus, ActionType, Direction
from email_reader.memory import briefing as briefing_svc
from email_reader.memory.briefing import Briefing, Card, Pick, fallback, parse_model
from email_reader.memory.context import Ref
from email_reader.model_connections import ResolvedModel
from email_reader.models import (
    Account,
    ActionItem,
    AgentDecision,
    CalendarEvent,
    Email,
    Identity,
    Project,
    ProjectThread,
    Thread,
)
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space

ME = "me@b.ru"
NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
TODAY = briefing_svc.local_day(NOW)[0]


def _card(n, type_=ActionType.NEED_MY_REPLY, title="t", delegate_to=None, **kw):
    item = ActionItem(id=uuid.uuid4(), space_id=SPACE_ID, type=type_, title=title,
                      status=ActionStatus.OPEN, delegate_to=delegate_to)
    return Card(n=n, item=item, section=kw.get("section"), since=kw.get("since"),
                vip=kw.get("vip", False), ongoing=kw.get("ongoing", False), stats=None,
                projects=kw.get("projects", []))


def test_parse_model_maps_numbers_dedupes_and_caps():
    cards = [_card(i) for i in range(1, 8)]
    cards[3] = _card(4, ActionType.TO_DELEGATE, delegate_to="Борис")
    text = (
        'Вот брифинг: {"focus":[{"n":1,"why":"срок сегодня"},{"n":2,"why":"VIP"},'
        '{"n":3,"why":"x"},{"n":5,"why":"лишний, кап 3"}],'
        '"delegate":[{"n":4,"why":"техника"},{"n":1,"why":"дубль — уже в фокусе"},'
        '{"n":6,"to":"","why":"некому"},{"n":99,"to":"Кто-то","why":"нет такой"}],'
        '"later":[{"n":7,"why":"подождёт"},{"n":"abc","why":"мусор"}],"note":"  Спокойный день.  "}'
    )
    b = parse_model(text, cards, TODAY)
    assert b is not None and b.source == "model"
    assert [(p.item_id, p.why) for p in b.focus] == [
        (cards[0].item.id, "срок сегодня"), (cards[1].item.id, "VIP"), (cards[2].item.id, "x"),
    ]
    assert [(p.item_id, p.to) for p in b.delegate] == [(cards[3].item.id, "Борис")]
    assert [p.item_id for p in b.later] == [cards[6].item.id]
    assert b.note == "Спокойный день."


def test_parse_model_rejects_garbage():
    cards = [_card(1)]
    assert parse_model("не JSON", cards, TODAY) is None
    assert parse_model('{"focus":[],"delegate":[],"later":[]}', cards, TODAY) is None
    assert parse_model(None, cards, TODAY) is None


def test_fallback_keeps_board_order_and_caps():
    cards = [_card(1), _card(2, ActionType.TO_DELEGATE, delegate_to="Оля"), _card(3),
             _card(4, vip=True), _card(5), _card(6, ActionType.TO_DELEGATE, delegate_to="Борис")]
    b = fallback(cards, TODAY)
    assert [p.item_id for p in b.focus] == [cards[0].item.id, cards[2].item.id, cards[3].item.id]
    assert [(p.item_id, p.to) for p in b.delegate] == [
        (cards[1].item.id, "Оля"), (cards[5].item.id, "Борис"),
    ]
    assert [p.item_id for p in b.later] == [cards[4].item.id]
    assert b.focus[2].why == "VIP."
    assert b.source == "fallback"


def test_briefing_json_roundtrip():
    ref = Ref("card", str(uuid.uuid4()), "карточка «Смета»", "/item/x")
    b = Briefing(day=TODAY, focus=[Pick(uuid.uuid4(), "why", None, [ref])],
                 delegate=[Pick(uuid.uuid4(), "w", "Борис")], note="n", source="model",
                 model="m", generated_at=NOW, calendar=[(NOW, "Планёрка")])
    back = Briefing.from_json(b.to_json())
    assert back is not None and back.to_json() == b.to_json()
    assert back.focus[0].refs == [ref]
    assert Briefing.from_json("{}") is None


async def _seed(s):
    await mk_space(s, 1)
    await mk_space(s, 2)
    acc = Account(space_id=SPACE_ID, name="w", host="h", login=ME, folders=[])
    other = Account(space_id=SPACE_ID_2, name="p", host="h", login="p@b.ru", folders=[])
    s.add_all([acc, other, Identity(email=ME)])
    await s.flush()
    th = Thread(account_id=acc.id)
    s.add(th)
    await s.flush()
    s.add(Email(account_id=acc.id, thread_id=th.id, dedup_hash=uuid.uuid4().hex,
                direction=Direction.INBOUND, from_addr="petrov@romashka.ru",
                to_addrs=[{"email": ME}], cc_addrs=[], sent_at=NOW - timedelta(hours=2)))
    reply = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="Ответить Петрову",
                       status=ActionStatus.OPEN, counterparty="Пётр (petrov@romashka.ru)",
                       source_thread_id=th.id, created_at=NOW - timedelta(days=1))
    stuck = ActionItem(space_id=SPACE_ID, type=ActionType.WAITING_FOR_REPLY, title="Жду смету",
                       status=ActionStatus.OPEN, counterparty="Анна",
                       created_at=NOW - timedelta(days=10))
    fresh = ActionItem(space_id=SPACE_ID, type=ActionType.WAITING_FOR_REPLY, title="Жду ответ",
                       status=ActionStatus.OPEN, counterparty="Борис",
                       created_at=NOW - timedelta(days=1))
    handoff = ActionItem(space_id=SPACE_ID, type=ActionType.TO_DELEGATE, title="Оценка API",
                         status=ActionStatus.OPEN, counterparty="Пётр", delegate_to="Борис",
                         created_at=NOW - timedelta(days=2))
    s.add_all([reply, stuck, fresh, handoff])
    await s.flush()
    project = Project(space_id=SPACE_ID, name="Ромашка", prompt="Интеграция")
    s.add(project)
    await s.flush()
    s.add(ProjectThread(project_id=project.id, thread_id=th.id, source="manual"))
    s.add_all([
        CalendarEvent(account_id=acc.id, provider_uid="1", start_at=NOW + timedelta(hours=1),
                      title="Планёрка"),
        CalendarEvent(account_id=acc.id, provider_uid="2", start_at=NOW - timedelta(days=1),
                      title="Вчера"),
        CalendarEvent(account_id=acc.id, provider_uid="3", start_at=NOW + timedelta(hours=2),
                      title="Отменена", cancelled_at=NOW),
        CalendarEvent(account_id=other.id, provider_uid="4", start_at=NOW + timedelta(hours=3),
                      title="Личное"),
    ])
    await s.commit()
    return reply, stuck, fresh, handoff


async def test_gather_collects_today_stuck_waits_and_the_calendar(db):
    async with db() as s:
        reply, stuck, fresh, handoff = await _seed(s)
        cards, calendar = await briefing_svc.gather(s, SPACE_ID, NOW)
    titles = [c.item.title for c in cards]
    assert "Ответить Петрову" in titles and "Оценка API" in titles
    assert "Жду смету" in titles          # waited longer than a week
    assert "Жду ответ" not in titles       # a fresh wait is not briefing material
    assert [c.n for c in cards] == list(range(1, len(cards) + 1))
    assert cards[titles.index("Ответить Петрову")].ongoing is False
    assert [t for _, t in calendar] == ["Планёрка"]
    reply_card = cards[titles.index("Ответить Петрову")]
    assert reply_card.projects == ["Ромашка"]
    assert "проект: «Ромашка»" in reply_card.facts(TODAY)
    assert cards[titles.index("Жду смету")].projects == []


async def test_generate_asks_the_model_once_a_day_and_falls_back_without_one(db, monkeypatch):
    calls: list[str] = []

    async def fake_call(**kw):
        calls.append(kw["user"])
        assert kw["tool_schema"]["name"] == "record_briefing"
        return (
            BriefingResult.model_validate({
                "focus": [{"n": 1, "why": "Пётр ждёт со вчера", "refs": [2]}],
                "delegate": [{"n": 2, "to": "Борис", "why": "техника", "refs": []}],
                "later": [], "note": "Тихо.",
            }),
            Usage(1, 1),
        )

    rm = ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")

    async def fake_resolve(*a, **k):
        return rm

    monkeypatch.setattr(briefing_svc, "structured_call", fake_call)
    monkeypatch.setattr(briefing_svc, "resolve_for_space", fake_resolve)

    async with db() as s:
        await _seed(s)
        b = await briefing_svc.generate(s, SPACE_ID, now=NOW)
        await s.commit()
        assert b.source == "model" and b.model == "m"
        assert len(calls) == 1
        assert "КАЛЕНДАРЬ:" in calls[0] and "Планёрка" in calls[0]
        assert "«Ответить Петрову»" in calls[0] and "кому делегировать: Борис" in calls[0]
        items = await briefing_svc.hydrate(s, b)
        assert items[b.focus[0].item_id].title == "Ответить Петрову"
        assert items[b.delegate[0].item_id].title == "Оценка API"
        assert [p.to for p in b.delegate] == ["Борис"]
        assert b.note == "Тихо."
        # The pick points at the other card of the day, resolved to a chip.
        assert [r.id for r in b.focus[0].refs] == [str(b.delegate[0].item_id)]
        # Same day: cached, the model is not asked again; force regenerates.
        again = await briefing_svc.generate(s, SPACE_ID, now=NOW + timedelta(hours=3))
        assert again.to_json() == b.to_json() and len(calls) == 1
        await briefing_svc.generate(s, SPACE_ID, now=NOW + timedelta(hours=3), force=True)
        assert len(calls) == 2
        journal = (
            await s.execute(select(AgentDecision).where(AgentDecision.kind == "briefing"))
        ).scalars().all()
        assert [d.payload["source"] for d in journal] == ["model", "model"]

    rm.api_key = None
    async with db() as s:
        b = await briefing_svc.generate(s, SPACE_ID, now=NOW + timedelta(days=1), force=True)
        assert b.source == "fallback" and len(calls) == 2
        assert b.focus and b.delegate and b.delegate[0].to == "Борис"


def test_body_renders_the_groups_with_their_reasons():
    a = ActionItem(id=uuid.uuid4(), space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY,
                   title="Ответить Петрову", status=ActionStatus.OPEN, counterparty="Пётр")
    d = ActionItem(id=uuid.uuid4(), space_id=SPACE_ID, type=ActionType.TO_DELEGATE,
                   title="Оценка API", status=ActionStatus.OPEN, delegate_to="Борис")
    gone = uuid.uuid4()
    b = Briefing(day=TODAY, focus=[Pick(a.id, "VIP, срок сегодня"), Pick(gone, "закрыта утром")],
                 delegate=[Pick(d.id, "техника", "Борис")], note="Спокойный день.",
                 source="model", model="m", generated_at=NOW, calendar=[(NOW, "Планёрка")])
    html = templates.env.get_template("_briefing_body.html").render(
        briefing=b, items={a.id: a, d.id: d}, day_text="05.09.2026",
    )
    assert "Брифинг на 05.09.2026" in html
    assert "Собрал ассистент (m)" in html
    assert "Спокойный день." in html and "Планёрка" in html
    assert "Ответить Петрову" in html and "VIP, срок сегодня" in html
    assert "→ Борис. техника" in html
    assert "закрыта утром" not in html   # a card that is gone is not shown
    assert 'action="/briefing/refresh"' in html


def test_parse_model_resolves_the_cards_a_reason_points_at():
    cards = [_card(i, title=f"Карточка {i}") for i in range(1, 7)]
    text = (
        '{"focus":[{"n":1,"why":"тот же человек ждёт по двум другим","refs":[2,3,2,1,99,"x"]},'
        '{"n":4,"why":"без улик","refs":[]}],'
        '"delegate":[],"later":[{"n":5,"why":"мусор в refs","refs":"не список"}],"note":"n"}'
    )
    b = parse_model(text, cards, TODAY)
    assert b is not None
    # Its own number, a repeat, a number the input never had and a non-number are dropped.
    assert [r.id for r in b.focus[0].refs] == [str(cards[1].item.id), str(cards[2].item.id)]
    assert b.focus[0].refs[0] == Ref(
        "card", str(cards[1].item.id), "карточка «Карточка 2»", f"/item/{cards[1].item.id}",
        short="Карточка 2",
    )
    assert b.focus[1].refs == [] and b.later[0].refs == []


def test_parse_model_caps_the_chips_after_one_reason():
    cards = [_card(i) for i in range(1, 8)]
    text = '{"focus":[{"n":1,"why":"w","refs":[2,3,4,5,6]}],"delegate":[],"later":[],"note":""}'
    b = parse_model(text, cards, TODAY)
    assert len(b.focus[0].refs) == briefing_svc.MAX_REFS == 3


def test_body_renders_evidence_chips_next_to_the_reason():
    a = ActionItem(id=uuid.uuid4(), space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY,
                   title="Ответить Петрову", status=ActionStatus.OPEN, counterparty="Пётр")
    other_id = uuid.uuid4()
    ref = Ref("card", str(other_id), "карточка «Смета от Ромашки»", f"/item/{other_id}")
    b = Briefing(day=TODAY, focus=[Pick(a.id, "он же ждёт смету", None, [ref])],
                 source="model", model="m", generated_at=NOW)
    html = templates.env.get_template("_briefing_body.html").render(
        briefing=b, items={a.id: a}, day_text="05.09.2026",
    )
    assert "он же ждёт смету" in html
    assert f'hx-get="/item/{other_id}?view=briefing"' in html
    assert 'title="карточка «Смета от Ромашки»"' in html
    assert '>карточка</a>' in html


def test_a_handoff_nobody_took_says_so_in_its_facts():
    card = _card(1, ActionType.TO_DELEGATE, title="Оценка API")
    card.item.updated_at = NOW - timedelta(days=9)
    assert "ответственный не назначен 9 дн." in card.facts(TODAY)
    # Свежий запрос молчит — «кому?» в строке и так видно.
    card.item.updated_at = NOW - timedelta(days=1)
    assert not any("ответственный" in f for f in card.facts(TODAY))
    # Ответственный назван — говорим об этом, а не о его отсутствии.
    with_owner = _card(2, ActionType.TO_DELEGATE, delegate_to="Борис")
    with_owner.item.updated_at = NOW - timedelta(days=9)
    assert with_owner.facts(TODAY) == ["кому делегировать: Борис"]


def test_thread_states_reach_the_prompt_the_json_and_the_page():
    """До 19.09 здесь была секция «Проекты без движения» по `projects.stalled`; теперь
    брифинг показывает Нити по состоянию — свёртке их обязательств (feature-topics.md
    §8.5). В промпт идут только требующие внимания, на страницу — все с открытым."""
    idle = ThreadAttention(uuid.uuid4(), "Ромашка", "idle", on_me=0, theirs=2, idle=2,
                           open_cards=3, quiet_days=18)
    flowing = ThreadAttention(uuid.uuid4(), "Спрос", "flow", on_me=0, theirs=1,
                              open_cards=1, quiet_days=1)
    text = briefing_svc.render_input([_card(1)], [], TODAY, [idle, flowing])
    assert ("НИТИ (посчитано по данным): «Ромашка» — затухла: на мне 0, чужой ход 2, "
            "тишина 18 дн.") in text
    assert "Спрос" not in text
    assert "НИТИ" not in briefing_svc.render_input([_card(1)], [], TODAY)

    b = Briefing(day=TODAY, source="model", model="m", generated_at=NOW,
                 threads=[idle, flowing])
    back = Briefing.from_json(b.to_json())
    assert back.threads == [idle, flowing]

    a = ActionItem(id=uuid.uuid4(), space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY,
                   title="Ответить", status=ActionStatus.OPEN)
    html = templates.env.get_template("_briefing_body.html").render(
        briefing=b, items={a.id: a}, day_text="13.09.2026",
    )
    assert "Нити — 2" in html
    assert f'href="/project/{idle.project_id}"' in html
    assert 'class="st-pill idle">затухла</span>' in html
    assert "тишина 18 дн." in html and 'class="st-pill flow">идёт</span>' in html
