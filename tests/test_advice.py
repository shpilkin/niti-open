"""Advice on a card (docs/feature-card-advice.md): the gate on citations, the cache key,
the numbering of evidence, the journal row, the pane block."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import email_reader.api.app  # noqa: F401  — registers icon/age/local on the template env
from email_reader import advice
from email_reader.ai.extractor import Usage
from email_reader.ai.schemas import AdviceResult, Claim, Recommendation
from email_reader.api.common import templates
from email_reader.enums import ActionStatus, ActionType, Direction
from email_reader.memory.context import (
    CardLine,
    FactLine,
    MemoryContext,
    PersonCard,
    Precedent,
    ProjectDossier,
    Ref,
)
from email_reader.model_connections import ResolvedModel
from email_reader.models import ActionItem, AgentDecision, Email, Identity
from tests.conftest import SPACE_ID
from tests.test_memory_context import ME, THEM, _account, _mail, _thread

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
D = timedelta(days=1)

REFS = [
    Ref("email", "e1", "Пётр, письмо от 10.09.2026", "/email/e1", short="Пётр, 10.09"),
    Ref("card", "c1", "карточка «Смета»", "/item/c1", short="Смета"),
    Ref("meeting", "m1", "встреча «Синк»", "/meeting/m1", short="Синк"),
]


def test_vet_keeps_only_what_stands_on_evidence():
    result = AdviceResult(
        importance=Claim(text="Он ждёт смету с 10.09", refs=[1]),
        recommendation=Recommendation(kind="close_duplicate", why="то же самое", refs=[2, 2, 7],
                                      target=2),
        points=[Claim(text="сроки", refs=[1, 1]), Claim(text="без улик", refs=[]),
                Claim(text="три"), Claim(text="четыре — лишний")],
        risk=Claim(text="срок 20.09", refs=[3]),
    )
    got = advice.vet(result, REFS, key="k")
    assert got.importance_why.text == "Он ждёт смету с 10.09"
    assert got.importance_why.refs == [REFS[0]]
    assert got.recommendation == "close_duplicate" and got.label == "Закрыть как дубль"
    assert got.refs == [REFS[1]]                      # deduped, the stray 7 dropped
    assert got.target == REFS[1]                      # a card — resolves
    assert [p.text for p in got.points] == ["сроки", "без улик", "три"]  # capped at three
    assert got.points[0].refs == [REFS[0]] and got.points[1].refs == []  # points may stand alone
    assert got.risk is not None and got.risk.refs == [REFS[2]]
    assert not got.is_empty


def test_vet_degrades_a_bad_target_and_an_unknown_kind():
    # `bundle` pointing at a letter, not a card: the kind stays, the target does not.
    got = advice.vet(AdviceResult(recommendation=Recommendation(kind="bundle", why="w", target=1)),
                     REFS, key="k")
    assert got.recommendation == "bundle" and got.target is None
    # A kind the schema does not know is no recommendation at all — nor its why.
    got = advice.vet(AdviceResult(recommendation=Recommendation(kind="panic", why="w", refs=[1])),
                     REFS, key="k")
    assert got.recommendation is None and got.why == "" and got.refs == []
    assert got.is_empty


def test_schema_tolerates_sloppy_numbers():
    r = AdviceResult.model_validate({
        "importance": {"text": "x", "refs": ["2", None, "no"]},
        "recommendation": {"kind": "reply", "why": "w", "refs": None, "target": "3"},
        "points": [], "risk": "",
    })
    assert r.importance.refs == [2]
    assert r.recommendation.refs == [] and r.recommendation.target == 3
    assert r.risk is None


def _item(**kw) -> ActionItem:
    it = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="Ответить про сроки",
                    counterparty="Пётр Петров", status=ActionStatus.OPEN, **kw)
    it.id = uuid.uuid4()
    return it


def _letter(direction=Direction.INBOUND) -> Email:
    return Email(direction=direction, from_addr=THEM, to_addrs=[], cc_addrs=[], sent_at=NOW)


def test_importance_is_a_rule_over_the_card_not_the_model_opinion():
    today = NOW.date()
    # Просрочено или срок на носу — высокая, чем бы карточка ни была.
    overdue = _item()
    overdue.status = ActionStatus.OVERDUE.value
    assert advice.importance(overdue, _letter(), now=NOW) == "high"
    assert advice.importance(_item(due_date=today + 2 * D), _letter(), now=NOW) == "high"
    # VIP-контрагент — высокая и без срока.
    assert advice.importance(_item(), _letter(), vip=True, now=NOW) == "high"
    # Срок есть, но не скоро — средняя.
    assert advice.importance(_item(due_date=today + 5 * D), _letter(), now=NOW) == "medium"
    # Ни срока, ни ожидания: последним писал я — мяч не у меня.
    assert advice.importance(_item(), _letter(Direction.OUTBOUND), now=NOW) == "low"
    # Ждут меня — средняя.
    assert advice.importance(_item(), _letter(), now=NOW) == "medium"


def test_long_lines_are_cut_to_their_limit(caplog):
    long_why = "Морозов просит прислать смету и сроки по второму этапу, " * 6
    long_point = "подтвердить срок и рассказать, что именно будет сделано к пятнице, " * 3
    got = advice.vet(
        AdviceResult(
            recommendation=Recommendation(kind="reply", why=long_why, refs=[1]),
            importance=Claim(text="Срок завтра в 16:00, после него выгрузка. " * 5, refs=[1]),
            points=[Claim(text=long_point, refs=[1])],
            risk=Claim(text="Смета уйдёт без правок. " * 10, refs=[1]),
        ),
        REFS, key="k",
    )
    assert len(got.why) <= advice.WHY_CHARS and got.why.endswith("…")
    assert len(got.importance_why.text) <= advice.IMPORTANCE_CHARS
    assert len(got.points[0].text) <= advice.POINT_CHARS
    assert len(got.risk.text) <= advice.RISK_CHARS
    # Обрезка — на границе слова, а не посреди.
    assert not got.why.removesuffix("…").endswith(" ")
    assert " ".join(got.why.split()) == got.why


def test_importance_without_evidence_is_not_shown():
    got = advice.vet(
        AdviceResult(
            recommendation=Recommendation(kind="reply", why="просит смету", refs=[1]),
            importance=Claim(text="мне кажется, это срочно", refs=[]),
        ),
        REFS, key="k",
    )
    assert got.importance_why is None and got.recommendation == "reply"


def test_key_names_the_card_the_thread_and_the_evidence():
    it = _item()
    base = advice._key(it, NOW, "улики")
    assert base == advice._key(_item(), NOW, "улики")
    assert base != advice._key(it, NOW + D, "улики")
    assert base != advice._key(it, NOW, "другие улики")
    retyped = _item()
    retyped.type = ActionType.WAITING_FOR_REPLY
    assert base != advice._key(retyped, NOW, "улики")
    dated = _item()
    dated.due_date = date(2026, 9, 20)
    assert base != advice._key(dated, NOW, "улики")


def _email(direction, body, days_ago, who="petrov@romashka.ru"):
    e = Email(direction=direction, from_addr=who, from_name="Пётр", to_addrs=[], cc_addrs=[],
              dedup_hash="h", sent_at=NOW - days_ago * D, body_text=body)
    e.id = uuid.uuid4()
    return e


def test_numbered_tail_is_oldest_first_and_counts_from_start():
    emails = [_email(Direction.INBOUND, "вопрос", 3), _email(Direction.OUTBOUND, "ответ", 2),
              _email(Direction.INBOUND, "ещё вопрос", 1)]
    text, refs = advice._numbered_tail(emails, start=4)
    assert [r.id for r in refs] == [str(e.id) for e in emails]
    assert text.startswith("[4] [Пётр — 10.09.2026]\nвопрос")
    assert "[5] [Я — 11.09.2026]\nответ" in text
    assert text.index("[4]") < text.index("[5]") < text.index("[6]")
    first = emails[0].id
    assert refs[0] == Ref("email", str(first), "Пётр, письмо от 10.09.2026", f"/email/{first}",
                          short="Пётр, 10.09")
    assert refs[1].label == "Я, письмо от 11.09.2026" and refs[1].short == "Я, 11.09"


def test_evidence_numbers_every_line_that_stands_on_a_row():
    card = CardLine(id=uuid.uuid4(), type="WAITING_FOR_REPLY", title="Смета", counterparty="Пётр",
                    due_date=None, status="open")
    ctx = MemoryContext(
        people=[PersonCard(email=THEM, name="Пётр Петров", vip=False, notes="CTO",
                           facts=["Пишет мне: 3"])],
        projects=[("Ромашка", "Интеграция")],
        precedents=[Precedent(when=NOW - 30 * D, subject="Доступы", outcome="я ответил(а)",
                              thread_id=uuid.uuid4())],
        facts=[FactLine(id=uuid.uuid4(), text="согласую с юристами")],
        cards_with=[card],
        dossiers={"Ромашка": ProjectDossier(name="Ромашка", siblings=["бюджет q4"])},
    )
    text, refs = ctx.evidence(start=3)
    numbered = [line for line in text.splitlines() if line.lstrip().startswith("[")]
    # The person's three lines, the card, the precedent, the fact — the project line, its
    # siblings and the headings stand on no row and get no number.
    assert len(numbered) == len(refs) == 6
    assert numbered[0].startswith("[3] КОНТРАГЕНТ: Пётр Петров")
    assert numbered[-1] == "  [8] - согласую с юристами"
    assert "    другие переписки: бюджет q4" in text and "[9]" not in text
    assert [r.kind for r in refs] == ["person", "person", "person", "card", "thread", "fact"]
    assert refs[3].url == f"/item/{card.id}" and refs[4].url.startswith("/thread/")
    assert text.startswith("ЧТО Я ЗНАЮ ВОКРУГ КАРТОЧКИ")
    # The extractor's block is untouched by numbering.
    assert "[3]" not in ctx.render("extract") and "ОТКРЫТОЕ С" not in ctx.render("extract")


async def test_advice_is_journaled_and_reused_while_the_key_holds(db, monkeypatch):
    from sqlalchemy import select

    calls: list[str] = []

    async def fake_call(**kw):
        calls.append(kw["user"])
        return (
            AdviceResult(
                importance=Claim(text="Он ждёт смету", refs=[1]),
                recommendation=Recommendation(kind="reply", why="прямой вопрос", refs=[1]),
                points=[Claim(text="сроки", refs=[1])],
            ),
            Usage(10, 5),
        )

    monkeypatch.setattr(advice, "structured_call", fake_call)
    rm = ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")
    async with db() as s:
        acc = await _account(s)
        s.add(Identity(email=ME))
        cur = await _thread(s, acc)
        mail = _mail(acc, cur, direction=Direction.INBOUND, from_addr=THEM,
                     from_name="Пётр Петров", to=[(ME, None)], at=NOW - D, subject="Смета")
        s.add(mail)
        item = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY,
                          title="Ответить про смету", counterparty="Пётр Петров",
                          status=ActionStatus.OPEN, source_thread_id=cur.id)
        s.add(item)
        await s.commit()

        prep = await advice.gather(s, item, now=NOW)
        assert prep is not None
        assert prep.refs[0] == Ref("email", str(mail.id), "Пётр Петров, письмо от 12.09.2026",
                                   f"/email/{mail.id}", short="Пётр, 12.09")
        assert prep.user.startswith(
            "СЕГОДНЯ: 13.09.2026\nКАРТОЧКА: [Нужен ответ] «Ответить про смету» — Пётр Петров"
        )
        assert "ПОСЛЕДНЕЕ ПИСЬМО: 12.09.2026, от Пётр Петров; с тех пор 1 дн." in prep.user
        assert "[1] [Пётр Петров — 12.09.2026]" in prep.user
        assert "[2] КОНТРАГЕНТ: Пётр Петров" in prep.user
        assert await advice.cached(s, item.id, prep.key) is None

        result, usage = await advice.generate(rm, prep)
        adv = advice.vet(result, prep.refs, prep.key)
        await advice.record(s, item, adv, rm, usage, now=NOW)
        await s.commit()

        again = await advice.cached(s, item.id, prep.key)
        assert again is not None and again.cached and again.model == "m"
        assert again.recommendation == "reply" and again.why == "прямой вопрос"
        assert again.importance_why.refs == [prep.refs[0]]
        row = (await s.execute(
            select(AgentDecision).where(AgentDecision.kind == advice.KIND)
        )).scalar_one()
        assert row.action_item_id == item.id
        assert row.payload["recommendation"] == "reply" and row.payload["key"] == prep.key
        assert (row.tokens_in, row.tokens_out) == (10, 5)

        # The owner retitles the card: the key moves, the old advice is not served.
        item.title = "Другой заголовок"
        await s.commit()
        prep2 = await advice.gather(s, item, now=NOW)
        assert prep2.key != prep.key
        assert await advice.cached(s, item.id, prep2.key) is None
    assert len(calls) == 1


async def test_an_empty_advice_is_not_written_down(db, caplog):
    """Сбой модели не должен глушить совет по карточке навсегда: пустое в кэш не идёт."""
    from sqlalchemy import select

    rm = ResolvedModel(kind="openai", base_url=None, api_key="k", model="пустышка")
    async with db() as s:
        acc = await _account(s)
        th = await _thread(s, acc)
        item = _item(source_thread_id=th.id)
        s.add(item)
        await s.commit()

        await advice.record(s, item, advice.Advice(key="k"), rm, Usage(1, 1), now=NOW)
        await s.commit()
        assert (await s.execute(
            select(AgentDecision).where(AgentDecision.kind == advice.KIND)
        )).scalars().all() == []

        # А непустой — записывается, как и раньше.
        good = advice.Advice(key="k", recommendation="reply", why="просит смету")
        await advice.record(s, item, good, rm, Usage(1, 1), now=NOW)
        await s.commit()
        assert len((await s.execute(
            select(AgentDecision).where(AgentDecision.kind == advice.KIND)
        )).scalars().all()) == 1
    assert "came back empty" in caplog.text


def test_only_mail_backed_obligations_get_advice():
    assert advice.advisable(_item(source_thread_id=uuid.uuid4()))
    assert not advice.advisable(_item())  # a meeting or typed card: no thread
    info = _item(source_thread_id=uuid.uuid4())
    info.type = ActionType.INFO
    assert not advice.advisable(info)
    assert not advice.advisable(None)


def test_advice_block_renders_claims_with_evidence_chips():
    item = _item()
    adv = advice.Advice(
        key="k",
        importance_why=advice.Claim("Он ждёт смету", [REFS[0]]),
        recommendation="bundle", why="одно письмо на оба", refs=[REFS[1]], target=REFS[1],
        points=[advice.Claim("сроки", [REFS[0]]), advice.Claim("кто ведёт", [])],
        risk=advice.Claim("срок 20.09", [Ref("fact", "f1", "память: срок 20.09", "",
                                              short="срок 20.09")]),
        generated_at=NOW,
    )
    html = templates.env.get_template("_advice.html").render(
        item=item, advice=adv, view="today", no_model=False,
        importance="high", importance_label="высокая",
    )
    assert "<b>Ответить вместе с</b>" in html and "одно письмо на оба" in html
    # Чип говорит, КАКАЯ это улика, а не какого она вида.
    assert ">Пётр, 10.09</a>" in html and ">Смета</a>" in html
    assert ">письмо<" not in html and ">карточка<" not in html
    assert 'hx-get="/item/c1?view=today"' in html                 # a card opens in the pane
    assert f'hx-get="/email/e1?back=/item/{item.id}%3Fview%3Dtoday"' in html  # with the way back
    # A fact has no pane: a chip without a link, printing the fact itself, not «память».
    assert '<span class="ev" title="память: срок 20.09">' in html
    assert "срок 20.09</span>" in html
    assert "Сделать:" in html and "кто ведёт" in html
    assert '<span class="st-pill high">Важность: высокая</span>' in html
    assert f'hx-get="/item/{item.id}/advice?view=today&refresh=1"' in html
    assert "13.09 15:00" in html or "13.09 12:00" in html  # generated_at, local time

    quiet = templates.env.get_template("_advice.html").render(
        item=item, advice=None, view="today", no_model=True,
        importance="low", importance_label="низкая",
    )
    assert "Модель для пространства не настроена" in quiet
    empty = templates.env.get_template("_advice.html").render(
        item=item, advice=advice.Advice(key="k"), view="today", no_model=False,
        importance="low", importance_label="низкая",
    )
    # Пустой ответ больше не кэшируется, поэтому строка зовёт пересчитать, а не сообщает,
    # что улик нет: улики были — их не использовала модель.
    assert "не дала ни одного утверждения" in empty
    assert f'hx-get="/item/{item.id}/advice?view=today&refresh=1"' in empty


async def test_draft_takes_the_cached_advice_as_its_plan(db, monkeypatch):
    from email_reader import drafts

    async def fake_call(**kw):
        return (
            AdviceResult(
                importance=Claim(text="Он ждёт смету", refs=[1]),
                recommendation=Recommendation(kind="reply", why="прямой вопрос", refs=[1]),
                points=[Claim(text="сроки", refs=[1]), Claim(text="кто ведёт", refs=[])],
            ),
            Usage(1, 1),
        )

    monkeypatch.setattr(advice, "structured_call", fake_call)
    rm = ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")
    async with db() as s:
        acc = await _account(s)
        s.add(Identity(email=ME))
        cur = await _thread(s, acc)
        s.add(_mail(acc, cur, direction=Direction.INBOUND, from_addr=THEM,
                    from_name="Пётр Петров", to=[(ME, None)], at=NOW - D, subject="Смета"))
        item = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY,
                          title="Ответить про смету", counterparty="Пётр Петров",
                          status=ActionStatus.OPEN, source_thread_id=cur.id)
        s.add(item)
        await s.commit()
        # No advice yet: the draft is written from the thread alone.
        assert (await drafts.gather(s, item)).plan == []
        # The draft is gathered at the real clock, so the advice must be too: «СЕГОДНЯ» is
        # part of the evidence key, and advice for another day is rightly not reused.
        prep = await advice.gather(s, item)
        result, usage = await advice.generate(rm, prep)
        await advice.record(s, item, advice.vet(result, prep.refs, prep.key), rm, usage)
        await s.commit()
        dctx = await drafts.gather(s, item)
        assert dctx.plan == ["сроки", "кто ведёт"]
        assert "План ответа" in drafts._user_prompt(dctx)
        # The thread moves on: the advice no longer stands on the same evidence, no plan.
        s.add(_mail(acc, cur, direction=Direction.INBOUND, from_addr=THEM,
                    from_name="Пётр Петров", to=[(ME, None)], at=NOW, subject="Re: Смета"))
        await s.commit()
        assert (await drafts.gather(s, item)).plan == []


async def test_close_as_duplicate_leaves_the_board_without_teaching_the_eval_set(db):
    from sqlalchemy import select

    from email_reader import history, views
    from email_reader.models import EvalSample

    async with db() as s:
        acc = await _account(s)
        cur = await _thread(s, acc)
        keeper = ActionItem(space_id=SPACE_ID, type=ActionType.WAITING_FOR_REPLY,
                            title="Смета от Ромашки", counterparty="Пётр Петров",
                            status=ActionStatus.OPEN, source_thread_id=cur.id)
        dup = ActionItem(space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY,
                         title="Смета (дубль)", counterparty="Пётр Петров",
                         status=ActionStatus.OPEN, source_thread_id=cur.id)
        s.add_all([keeper, dup])
        await s.commit()

        await views.close_duplicate(s, dup.id, keeper.id)
        await s.commit()
        assert dup.status == ActionStatus.DISMISSED and dup.resolved_by == "user"
        assert dup.user_edited and dup.user_verdict is None  # not «не обязательство»
        assert keeper.status == ActionStatus.OPEN
        assert (await s.execute(select(EvalSample))).scalars().all() == []
        row = (await s.execute(select(AgentDecision).where(
            AgentDecision.action_item_id == dup.id))).scalar_one()
        assert row.payload == {"action": "duplicate", "of": str(keeper.id),
                               "of_title": "Смета от Ромашки"}
        entries = await history.for_item(s, dup.id)
        assert [(e.text, e.actor) for e in entries] == [
            ("Закрыта как дубль карточки «Смета от Ромашки»", "user"),
        ]
        # No target, or itself: nothing happens.
        await views.close_duplicate(s, keeper.id, None)
        await views.close_duplicate(s, keeper.id, keeper.id)
        assert keeper.status == ActionStatus.OPEN


def test_a_line_shows_two_chips_and_counts_the_rest():
    """Четыре чипа подряд превращают строку в частокол."""
    item = _item()
    many = REFS + [Ref("thread", "t1", "тред «Сроки»", "/thread/t1", short="Сроки")]
    adv = advice.Advice(key="k", recommendation="reply", why="просит смету", refs=many)
    html = templates.env.get_template("_advice.html").render(
        item=item, advice=adv, view="today", no_model=False,
        importance="medium", importance_label="средняя",
    )
    assert html.count('class="ev"') == 3  # два чипа-улики и «ещё 2»
    assert "ещё 2" in html
    assert "встреча «Синк»" in html  # что скрыто — видно в подсказке


def test_advice_block_offers_the_click_only_for_a_duplicate_with_a_card():
    item = _item()
    dup = advice.Advice(key="k", recommendation="close_duplicate", why="то же", refs=[REFS[1]],
                        target=REFS[1])
    html = templates.env.get_template("_advice.html").render(
        item=item, advice=dup, view="today", no_model=False,
    )
    assert f'hx-post="/item/{item.id}/duplicate"' in html
    assert '"target":"c1"' in html and "Закрыть как дубль</button>" in html
    # The same kind without a resolvable card: text only.
    text_only = advice.Advice(key="k", recommendation="close_duplicate", why="то же")
    html = templates.env.get_template("_advice.html").render(
        item=item, advice=text_only, view="today", no_model=False,
    )
    assert "/duplicate" not in html and "<b>Закрыть как дубль</b>" in html
    bundle = advice.Advice(key="k", recommendation="bundle", why="вместе", target=REFS[1])
    html = templates.env.get_template("_advice.html").render(
        item=item, advice=bundle, view="today", no_model=False,
    )
    assert "/duplicate" not in html


def test_vet_drops_the_numbers_the_model_writes_into_the_text():
    """Модель всё равно пишет «[1]» внутри фразы, хотя просили только в refs. Номер в
    тексте показывает пользователю внутреннюю нумерацию промпта — и ссылкой не является:
    рядом стоит чип. Тайм-код встречи под шаблон не попадает."""
    result = AdviceResult(
        importance=Claim(text="Соколов просит [1], срок был [12] и ещё [3, 7]", refs=[1]),
        points=[Claim(text="на встрече [00:08:23] решили иначе", refs=[])],
    )
    got = advice.vet(result, REFS, key="k")
    assert got.importance_why.text == "Соколов просит, срок был и ещё"
    assert got.points[0].text == "на встрече [00:08:23] решили иначе"


def test_card_head_says_the_status_in_russian():
    it = _item(source_thread_id=uuid.uuid4())
    it.status = ActionStatus.OVERDUE
    mail = _email(Direction.INBOUND, "вопрос", 2)
    assert "; просрочена" in advice._head(it, [mail], NOW)
    it.status = ActionStatus.OPEN
    head = advice._head(it, [mail], NOW)
    assert "статус" not in head and "overdue" not in head
    assert "ПОСЛЕДНЕЕ ПИСЬМО: 11.09.2026, от Пётр; с тех пор 2 дн." in head


async def test_a_new_letter_in_the_thread_makes_the_advice_stale(db, monkeypatch):
    """Совет написан по состоянию на момент разбора. Пришёл ответ — прежний вывод может
    быть уже неверен («жду ответа», когда ответ лежит в треде), и переиспользовать его
    нельзя: ключ считается от последнего письма и текста переписки."""
    calls: list[str] = []

    async def fake_call(**kw):
        calls.append(kw["user"])
        return (
            AdviceResult(
                importance=Claim(text="Он ждёт смету", refs=[1]),
                recommendation=Recommendation(kind="reply", why="прямой вопрос", refs=[1]),
                points=[Claim(text="сроки", refs=[1])],
            ),
            Usage(10, 5),
        )

    monkeypatch.setattr(advice, "structured_call", fake_call)
    rm = ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")
    async with db() as s:
        acc = await _account(s)
        s.add(Identity(email=ME))
        cur = await _thread(s, acc)
        s.add(_mail(acc, cur, direction=Direction.INBOUND, from_addr=THEM,
                    from_name="Пётр Петров", to=[(ME, None)], at=NOW - D, subject="Смета"))
        item = ActionItem(space_id=SPACE_ID, type=ActionType.WAITING_FOR_REPLY,
                          title="Жду смету", counterparty="Пётр Петров",
                          status=ActionStatus.OPEN, source_thread_id=cur.id)
        s.add(item)
        await s.commit()

        prep = await advice.gather(s, item, now=NOW)
        result, usage = await advice.generate(rm, prep)
        await advice.record(s, item, advice.vet(result, prep.refs, prep.key), rm, usage)
        await s.commit()
        assert await advice.cached(s, item.id, prep.key) is not None  # свежий — берётся

        # Пётр ответил. Тот же совет к этому треду больше не относится.
        reply = _mail(acc, cur, direction=Direction.INBOUND, from_addr=THEM,
                      from_name="Пётр Петров", to=[(ME, None)], at=NOW, subject="Смета")
        reply.body_text = "Готово, отправил."
        s.add(reply)
        await s.commit()

        after = await advice.gather(s, item, now=NOW)
        assert after.key != prep.key
        assert await advice.cached(s, item.id, after.key) is None  # пересчитается
        assert "Готово, отправил." in after.user  # и увидит новое письмо
