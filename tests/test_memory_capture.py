"""Capture: my letters and my meetings become proposals, the gate keeps out what the
source does not say, and a proposal is not memory until the owner says yes
(docs/feature-memory-facts.md §3.7, §6)."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

import email_reader.api.app  # noqa: F401  — registers icon/local on the template env
from email_reader import meetings, profiles
from email_reader.enums import Direction, EventStatus, EventType
from email_reader.memory import capture, facts
from email_reader.memory import context as memory_ctx
from email_reader.memory import people as people_svc
from email_reader.model_connections import ResolvedModel
from email_reader.models import Account, AgentDecision, Email, Event, MemoryFact
from email_reader.search import query as search_query
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
D = timedelta(days=1)
LETTER = (
    "Борис, бэкенд и интеграции на тебе, оценки и ТЗ тоже. Оля Волкова ведёт метрики "
    "и дашборды, с ней по цифрам. Счета от подрядчиков — сразу в бухгалтерию."
)


async def _account(s, n=1):
    await mk_space(s, n)
    acc = Account(space_id=uuid.UUID(int=n), name=f"w{n}", host="h", login=f"me{n}@b.ru",
                  folders=[])
    s.add(acc)
    await s.flush()
    return acc


def _mail(acc, *, direction=Direction.OUTBOUND, body=LETTER, at=NOW - D, low=False,
          subject="Кто за что"):
    return Email(
        account_id=acc.id, dedup_hash=uuid.uuid4().hex, direction=direction,
        from_addr=acc.login, from_name="Я",
        to_addrs=[{"name": "Борис Иванов", "email": "boris@x.ru"}], cc_addrs=[],
        subject=subject, body_text=body, sent_at=at, is_low_value=low,
    )


def _model(monkeypatch, replies):
    """`ask` answers from the list in order and keeps what it was asked."""
    asked: list[str] = []

    async def fake_model(session, space_id):  # noqa: ANN001
        return ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")

    async def fake_ask(rm, user):  # noqa: ANN001
        asked.append(user)
        return replies.pop(0) if replies else {"facts": []}

    monkeypatch.setattr(capture, "model_for", fake_model)
    monkeypatch.setattr(capture, "ask", fake_ask)
    return asked


def _fact(**over):
    base = {"about": "person", "who": "Борис Иванов", "kind": "responsibility",
            "text": "Борис отвечает за бэкенд и интеграции",
            "quote": "бэкенд и интеграции на тебе", "source": "m1", "confidence": 0.9}
    return {**base, **over}


async def _events(s):
    return list((await s.execute(
        select(Event).where(Event.type == str(EventType.FACTS_CAPTURE)).order_by(Event.id)
    )).scalars().all())


# --- the gate ---------------------------------------------------------------------


def test_the_gate_keeps_what_the_source_does_not_say():
    """An id copied back is not evidence; a sentence that is there is. The quote is
    looked for in the text — case and ё aside — and lands in the source it is really
    in, whatever the model named."""
    m1 = capture.Source(
        key="m1", evidence_kind="mail", evidence_id="a", at=NOW,
        text="[m1] 11.09.2026, от меня — кому: Борис Иванов <boris@x.ru>\n" + LETTER,
    )
    m2 = capture.Source(
        key="m2", evidence_kind="mail", evidence_id="b", at=NOW,
        text="[m2] 10.09.2026, от меня — кому: Пётр <p@x.ru>\nПётр, ты ведёшь закупки.",
    )
    chunk = [m1, m2]

    ok, why = capture.vet(_fact(), chunk)
    assert why == "" and ok["source"] is m1 and ok["confidence"] == 0.9
    ok, why = capture.vet(_fact(quote="БЭКЕНД и интеграции на тебе"), chunk)
    assert why == "" and ok["source"] is m1
    # a quote the letter never contained
    none, why = capture.vet(_fact(quote="Борис отвечает за дизайн и фронтенд"), chunk)
    assert none is None and why.startswith("quote not in source")
    # the right words under the wrong number: re-attributed, not dropped
    ok, why = capture.vet(_fact(who="Пётр", quote="ты ведёшь закупки", source="m1"), chunk)
    assert why == "" and ok["source"] is m2
    # a person the source never names, a kind outside the list, a subject that is neither
    for bad, reason in (
        (_fact(who="Роман"), "who not in source"),
        (_fact(kind="team"), "kind"),
        (_fact(about="team"), "about"),
        (_fact(quote="коротко"), "quote length"),
        (_fact(text=""), "text"),
        ("not a dict", "not an object"),
    ):
        none, why = capture.vet(bad, chunk)
        assert none is None and why.startswith(reason), (bad, why)
    # the header of a meeting names the participants — a name from there is in the source
    ok, why = capture.vet(_fact(who="Станислав Зайцев", quote="ты ведёшь закупки"), chunk,
                          header="Встреча. Участники: Станислав Зайцев.")
    assert why == ""


# --- a pass over mail ---------------------------------------------------------------


async def test_a_pass_reads_my_long_letters_of_the_space_and_proposes(db, monkeypatch):
    """Outbound, not low-value, long enough, this space's mailboxes — nothing else is
    sent. What comes back is proposed, not remembered: the prompt, the index and the
    memory block do not see it."""
    replies = [{"facts": [
        _fact(),
        _fact(about="me", who=None, kind="rule", text="Счета от подрядчиков идут в бухгалтерию",
              quote="Счета от подрядчиков — сразу в бухгалтерию", confidence=0.7),
        _fact(who="Оля Волкова", text="Оля Волкова ведёт метрики и дашборды",
              quote="Оля Волкова ведёт метрики и дашборды"),
        _fact(about="me", who=None, kind="role", text="Я — CTO", quote="я теперь CTO компании"),
    ]}]
    asked = _model(monkeypatch, replies)
    async with db() as s:
        work, home = await _account(s, 1), await _account(s, 2)
        mine = _mail(work)
        s.add_all([
            mine,
            _mail(work, direction=Direction.INBOUND, body="Борис отвечает за дизайн " * 5),
            _mail(work, low=True, body="рассылка о скидках " * 10),
            _mail(work, body="ок, спасибо", at=NOW - D + timedelta(minutes=5)),
            _mail(home, body="дома я отвечаю за дачу и её ремонт " * 4),
        ])
        await s.flush()
        boris = await people_svc.ensure_person(s, "boris@x.ru", "Борис Иванов")
        await profiles.set_profile(s, SPACE_ID, "Роль: PM.")
        await capture.set_on(s, SPACE_ID, True, now=NOW - 2 * D)
        await s.commit()

        await capture.process_facts_capture(s, {"space_id": str(SPACE_ID), "reason": "now"})
        await s.commit()

        assert len(asked) == 1
        head = "[m1] 11.09.2026, от меня — кому: Борис Иванов <boris@x.ru> — тема: Кто за что"
        assert head in asked[0]
        assert "[m2]" not in asked[0]
        assert "дизайн" not in asked[0] and "дачу" not in asked[0] and "скидках" not in asked[0]

        rows = (await s.execute(select(MemoryFact).order_by(MemoryFact.seq))).scalars().all()
        by_text = {r.text: r for r in rows}
        assert set(by_text) == {
            "Роль: PM.", "Борис отвечает за бэкенд и интеграции",
            "Счета от подрядчиков идут в бухгалтерию", "Оля Волкова ведёт метрики и дашборды",
        }
        about_boris = by_text["Борис отвечает за бэкенд и интеграции"]
        assert about_boris.status == "proposed" and about_boris.space_id is None
        assert about_boris.subject_key == boris.id.hex and about_boris.captured_in == SPACE_ID
        assert about_boris.evidence_kind == "mail" and about_boris.evidence_id == mine.id.hex
        assert about_boris.quote == "бэкенд и интеграции на тебе" and about_boris.confidence == 0.9
        rule = by_text["Счета от подрядчиков идут в бухгалтерию"]
        assert rule.subject_kind == "me" and rule.space_id == SPACE_ID and rule.status == "proposed"
        olya = by_text["Оля Волкова ведёт метрики и дашборды"]
        assert olya.subject_key is None and olya.subject_name == "Оля Волкова"

        # not memory yet: the prompt text, the index and the block are as before
        assert await profiles.get_profile(s, SPACE_ID) == "Роль: PM."
        _, hits = await search_query.search(s, "бухгалтерия", space_id=SPACE_ID, today=NOW.date())
        assert hits == []
        ctx = await memory_ctx.for_subject(
            s, space_id=SPACE_ID, people=[("boris@x.ru", "Борис Иванов")],
            query="счета подрядчиков бэкенд", now=NOW,
        )
        assert ctx.facts == [] and not ctx.people[0].notes

        journal = (await s.execute(
            select(AgentDecision).where(AgentDecision.kind == "fact_capture")
        )).scalar_one()
        assert journal.model == "m" and journal.payload["source"] == "mail"
        assert journal.payload["sources"] == [mine.id.hex]
        assert set(journal.payload["proposed"]) == {
            str(about_boris.id), str(rule.id), str(olya.id)
        }
        assert journal.payload["dropped"] == ["quote not in source: я теперь CTO компании"]
        # the watermark is past everything read, the short letter included
        wm = await capture.watermark(s, SPACE_ID, capture.MAIL_KEY)
        assert wm == NOW - D + timedelta(minutes=5)
        # and the next batch is queued; it finds nothing and queues nothing more
        assert [e.status for e in await _events(s)] == [EventStatus.PENDING]
        await capture.process_facts_capture(s, {"space_id": str(SPACE_ID), "reason": "now"})
        await s.commit()
        assert len(asked) == 1 and len(await _events(s)) == 1


async def test_the_same_thing_again_is_seen_and_a_no_is_remembered(db):
    async with db() as s:
        await mk_space(s)
        kw = dict(subject_kind="me", kind="rule", evidence_kind="mail", evidence_id="a",
                  quote="q" * 12, captured_in=SPACE_ID, space_id=SPACE_ID)
        verdict, row = await facts.propose(
            s, text="счета от подрядчиков — сразу в бухгалтерию", now=NOW, **kw
        )
        assert verdict == "proposed" and row.last_seen_at is None
        verdict, again = await facts.propose(
            s, text="Счета подрядчиков сразу отправляю в бухгалтерию", now=NOW + 3 * D, **kw
        )
        assert verdict == "seen" and again.id == row.id and again.last_seen_at == NOW + 3 * D
        await facts.reject(s, row, now=NOW + 4 * D)
        verdict, known = await facts.propose(
            s, text="счета от подрядчиков — в бухгалтерию", now=NOW + 5 * D, **kw
        )
        assert verdict == "known" and known.id == row.id
        # a line the owner remembered, then forgot, answers the same way
        line = await profiles.remember_line(s, SPACE_ID, "ТЗ присылать в Confluence", now=NOW)
        again = dict(text="ТЗ присылаю в Confluence", now=NOW, **kw)
        assert (await facts.propose(s, **again))[0] == "seen"
        await profiles.forget(s, SPACE_ID, line.id)
        assert (await facts.propose(s, **again))[0] == "known"
        # something else is news
        assert (await facts.propose(s, text="Борис ведёт бэкенд", now=NOW, **kw))[0] == "proposed"
        await s.commit()
        rows = (await s.execute(select(MemoryFact))).scalars().all()
    assert len(rows) == 3


async def test_the_profile_text_answers_line_by_line_not_as_one_blob(db):
    """The editor's text names every team; a move between two of them shares most of its
    stems with the blob and is still news — the very case capture is for (§2)."""
    async with db() as s:
        await mk_space(s)
        await profiles.set_profile(
            s, SPACE_ID,
            "Роль: PM.\nКоманды:\n- Аналитика (Оля, Роман) — метрики, дашборды.\n"
            "- Дизайн (Оля) — UX/UI, макеты.",
        )
        kw = dict(subject_kind="me", kind="member", evidence_kind="mail", evidence_id="a",
                  quote="q" * 12, captured_in=SPACE_ID, space_id=SPACE_ID, now=NOW)
        moved = await facts.propose(s, text="Оля ушла из аналитики в дизайн", **kw)
        assert moved[0] == "proposed"
        # …while what a line of the profile already says is not
        same = await facts.propose(s, text="Аналитика: Оля и Роман, метрики и дашборды", **kw)
        assert same[0] == "seen"
        await s.commit()


async def test_yes_makes_the_fact_live_indexed_and_part_of_the_prompt(db):
    async with db() as s:
        await _account(s)
        await profiles.set_profile(s, SPACE_ID, "Роль: PM.")
        kw = dict(subject_kind="me", evidence_kind="mail", evidence_id=uuid.uuid4().hex,
                  quote="q" * 12, captured_in=SPACE_ID, space_id=SPACE_ID, now=NOW)
        _, rule = await facts.propose(
            s, kind="rule", text="Счета от подрядчиков — сразу в бухгалтерию", **kw
        )
        _, role = await facts.propose(s, kind="role", text="Я — PM продукта", **kw)
        await s.commit()

        await capture.confirm(s, rule, now=NOW + D)
        await s.commit()
        assert rule.status == "active" and rule.confirmed_at == rule.valid_from == NOW + D
        assert await profiles.get_profile(s, SPACE_ID) == (
            "Роль: PM.\n- 2026-09-13: Счета от подрядчиков — сразу в бухгалтерию"
        )
        _, hits = await search_query.search(s, "бухгалтерия где:память", space_id=SPACE_ID,
                                            today=NOW.date())
        assert [r.ref for r in hits] == [rule.id.hex]
        ctx = await memory_ctx.for_subject(s, space_id=SPACE_ID, query="счета подрядчиков", now=NOW)
        assert [(f.id, f.matched) for f in ctx.facts] == [(rule.id, True)]

        # one role at a time: the next confirmed role retires this one
        await capture.confirm(s, role, now=NOW + 2 * D)
        _, cto = await facts.propose(s, kind="role", text="Я — CTO компании", **kw)
        await capture.confirm(s, cto, now=NOW + 3 * D)
        await s.commit()
        assert role.valid_to == NOW + 3 * D and cto.supersedes_id == role.id
        live = await facts.active(s, subject_kind="me", space_id=SPACE_ID)
        assert [f.text for f in live] == [
            "Роль: PM.", "Счета от подрядчиков — сразу в бухгалтерию", "Я — CTO компании",
        ]
        # «всё ещё так» on a live fact only stamps it
        await capture.confirm(s, rule, now=NOW + 10 * D)
        assert rule.confirmed_at == NOW + 10 * D and rule.valid_from == NOW + D
        # …which is what «устарело?» goes by
        assert [f.id for f in await facts.stale(s, SPACE_ID, now=NOW + 185 * D)] == [cto.id]
        assert await facts.stale(s, SPACE_ID, now=NOW + 100 * D) == []


async def test_yes_on_a_fact_about_a_stranger_makes_the_person(db):
    async with db() as s:
        await _account(s)
        _, f = await facts.propose(
            s, subject_kind="person", subject_name="Оля Волкова", kind="responsibility",
            text="Оля Волкова ведёт метрики", evidence_kind="mail", evidence_id="a",
            quote="q" * 12, captured_in=SPACE_ID, now=NOW,
        )
        await s.commit()
        assert await people_svc.get_person_by_name(s, "Оля Волкова") is None
        shown = (await capture.screen(s, SPACE_ID))["proposals"]
        assert [(p.about, p.new_person, p.kind_label) for p in shown] == [
            ("Оля Волкова", True, "зона ответственности")
        ]

        await capture.confirm(s, f, now=NOW)
        await s.commit()
        person = await people_svc.get_person_by_name(s, "Оля Волкова")
        assert person is not None and f.subject_key == person.id.hex and f.subject_name is None
        assert await people_svc.notes_of(s, person) == "2026-09-12: Оля Волкова ведёт метрики"
        ctx = await memory_ctx.for_subject(
            s, space_id=SPACE_ID, people=[(None, "Оля Волкова")], now=NOW
        )
        assert ctx.people[0].notes == "2026-09-12: Оля Волкова ведёт метрики"


# --- a meeting --------------------------------------------------------------------


async def test_a_meeting_is_read_by_its_turns_and_the_evidence_opens_at_the_moment(
    db, tmp_path, monkeypatch
):
    path = tmp_path / "ASROutput" / "rec.txt"
    path.parent.mkdir(parents=True)
    path.write_text(
        "[00:00:04] Спикер 1: Начали.\n"
        "[00:08:23] Спикер 2: Решили, что релиз ведёт Станислав, он техлид Alpha.\n",
        encoding="utf-8",
    )
    replies = [{"facts": [_fact(who="Станислав", kind="role", text="Станислав — техлид Alpha",
                                quote="он техлид Alpha", source="t1")]}]
    asked = _model(monkeypatch, replies)
    async with db() as s:
        await mk_space(s)
        m = await meetings.ingest_payload(s, {
            "meeting_id": "abc123", "started_at": "2026-09-05T10:00:00+00:00",
            "duration_sec": 600, "asr_root": str(tmp_path), "transcript_path": "ASROutput/rec.txt",
        })
        m.title, m.participants = "Планёрка", ["Станислав Зайцев"]
        await capture.set_on(s, SPACE_ID, True, now=NOW - 30 * D)
        await s.commit()

        await capture.process_facts_capture(s, {"space_id": str(SPACE_ID), "reason": "now"})
        await s.commit()

        assert asked[0].startswith("Встреча «Планёрка», 05.09.2026. Участники: Станислав Зайцев.")
        assert "[t1] [00:00:04] Спикер 1: Начали." in asked[0]
        assert "[t2] [00:08:23] Спикер 2: Решили" in asked[0]
        f = (await s.execute(select(MemoryFact))).scalar_one()
        assert f.status == "proposed" and f.evidence_kind == "turn"
        assert f.evidence_id == f"{m.id.hex}:503"  # the turn the quote is in, not «t1»
        assert f.subject_name == "Станислав" and f.subject_key is None
        shown = (await capture.screen(s, SPACE_ID))["proposals"][0]
        assert shown.url == f"/meeting/{m.id}?at=[00:08:23]"
        assert shown.source_label == "встреча «Планёрка» 05.09.2026 [00:08:23]"
        # read once: the next pass has nothing
        await capture.process_facts_capture(s, {"space_id": str(SPACE_ID), "reason": "now"})
    assert len(asked) == 1


# --- the switch, the queue, the failure path --------------------------------------------


async def test_off_means_nothing_runs_and_the_sweep_queues_one_event_per_space(db, monkeypatch):
    asked = _model(monkeypatch, [])
    async with db() as s:
        work = await _account(s, 1)
        await _account(s, 2)
        s.add(_mail(work))
        await s.flush()
        assert await capture.sweep(s) == 0
        await capture.process_facts_capture(s, {"space_id": str(SPACE_ID), "reason": "now"})
        assert asked == [] and await _events(s) == []

        await capture.set_on(s, SPACE_ID, True, now=NOW - 2 * D)
        assert await capture.sweep(s) == 1
        assert await capture.sweep(s) == 0  # one open event per space
        (ev,) = await _events(s)
        assert ev.payload == {"space_id": str(SPACE_ID), "reason": "sweep"}
        assert (await capture.screen(s, SPACE_ID))["capture"]["queued"] is True

        # switched off again: the queued event does nothing when its turn comes
        await capture.set_on(s, SPACE_ID, False)
        await capture.process_facts_capture(s, dict(ev.payload))
        assert asked == [] and len(await _events(s)) == 1


async def test_a_failed_pass_leaves_the_watermark_and_writes_nothing(db, monkeypatch):
    from email_reader.pipeline import worker

    @asynccontextmanager
    async def scope():
        async with db() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    monkeypatch.setattr(worker, "session_scope", scope)

    async def fake_model(session, space_id):  # noqa: ANN001
        return ResolvedModel(kind="openai", base_url=None, api_key="k", model="m")

    async def boom(rm, user):  # noqa: ANN001
        raise RuntimeError("model answered with garbage")

    monkeypatch.setattr(capture, "model_for", fake_model)
    monkeypatch.setattr(capture, "ask", boom)

    async with db() as s:
        work = await _account(s)
        s.add(_mail(work))
        await capture.set_on(s, SPACE_ID, True, now=NOW - 2 * D)
        assert await capture.request(s, SPACE_ID, "now")
        await s.commit()

    assert await worker.process_one() is True

    async with db() as s:
        (ev,) = await _events(s)
        assert ev.status == EventStatus.PENDING and ev.attempts == 1
        assert await capture.watermark(s, SPACE_ID, capture.MAIL_KEY) == NOW - 2 * D
        assert (await s.execute(select(MemoryFact))).scalars().all() == []
        assert (await s.execute(select(AgentDecision))).scalars().all() == []


async def test_backfill_moves_the_watermarks_back_and_the_screen_counts_the_calls(db):
    async with db() as s:
        work = await _account(s)
        for i in range(20):
            s.add(_mail(work, at=NOW - (i + 1) * D))
        await s.flush()
        await capture.set_on(s, SPACE_ID, True, now=NOW)
        await s.commit()
        state = (await capture.screen(s, SPACE_ID))["capture"]
        assert state["on"] and state["since"] == NOW and state["pending_calls"] == 0
        assert state["backfill_calls"] == 3  # 20 letters, 8 per call
        await capture.move_back(s, SPACE_ID, now=NOW)
        assert await capture.watermark(s, SPACE_ID, capture.MAIL_KEY) == NOW - capture.BACKFILL
        assert await capture.watermark(s, SPACE_ID, capture.MEETING_KEY) == NOW - capture.BACKFILL
        assert (await capture.screen(s, SPACE_ID))["capture"]["pending_calls"] == 3
        # a batch is eight letters, oldest first, and the watermark follows it
        batch = await capture.prepare(s, SPACE_ID, now=NOW)
        assert batch.kind == "mail"
        assert [x.key for x in batch.sources] == [f"m{i}" for i in range(1, 9)]
        assert batch.cursor == NOW - 13 * D


# --- the screen -------------------------------------------------------------------


async def test_the_switch_and_buttons_answer_with_the_block(db, monkeypatch):
    from starlette.requests import Request

    from email_reader.api.routers import settings as settings_router

    @asynccontextmanager
    async def scope():
        async with db() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    monkeypatch.setattr(settings_router, "session_scope", scope)
    async with db() as s:
        work = await _account(s)
        s.add(_mail(work, at=NOW - 10 * D))
        await s.commit()
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                       "query_string": b""})

    html = (await settings_router.profile_capture(request, SPACE_ID, action="on")).body.decode()
    assert f'id="memory-{SPACE_ID}"' in html and "<b>включён</b>" in html
    assert ">Выключить захват<" in html and "Разобрать новое" in html
    async with db() as s:
        assert await capture.is_on(s, SPACE_ID)
        since = await capture.watermark(s, SPACE_ID, capture.MAIL_KEY)
        assert since is not None and abs((datetime.now(UTC) - since).total_seconds()) < 60

    resp = await settings_router.profile_capture(request, SPACE_ID, action="backfill")
    html = resp.body.decode()
    assert "разбор в очереди" in html
    async with db() as s:
        moved = await capture.watermark(s, SPACE_ID, capture.MAIL_KEY)
        assert moved == since - capture.BACKFILL
        assert len(await _events(s)) == 1
    # a second click while the pass is queued does not queue a second pass
    await settings_router.profile_capture(request, SPACE_ID, action="now")
    async with db() as s:
        assert len(await _events(s)) == 1

    html = (await settings_router.profile_capture(request, SPACE_ID, action="off")).body.decode()
    assert "<b>выключен</b>" in html and "Разобрать новое" not in html
    # on again: the watermark is now again, not the archive point — what passed while it
    # was off is not read on the sly
    await settings_router.profile_capture(request, SPACE_ID, action="on")
    async with db() as s:
        planted = await capture.watermark(s, SPACE_ID, capture.MAIL_KEY)
        assert abs((datetime.now(UTC) - planted).total_seconds()) < 60


async def test_the_verdict_route_answers_with_the_block(db, monkeypatch):
    from starlette.requests import Request

    from email_reader.api.routers import settings as settings_router

    @asynccontextmanager
    async def scope():
        async with db() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    monkeypatch.setattr(settings_router, "session_scope", scope)
    async with db() as s:
        work = await _account(s, 1)
        await _account(s, 2)
        mail = _mail(work)
        s.add(mail)
        await s.flush()
        boris = await people_svc.ensure_person(s, "boris@x.ru", "Борис Иванов")
        kw = dict(kind="rule", evidence_kind="mail", evidence_id=mail.id.hex,
                  quote="бэкенд и интеграции на тебе", now=NOW)
        _, rule = await facts.propose(s, subject_kind="me", space_id=SPACE_ID, captured_in=SPACE_ID,
                                      text="Счета в бухгалтерию", **kw)
        _, about = await facts.propose(s, subject_kind="person", subject_key=boris.id.hex,
                                       captured_in=SPACE_ID, text="Борис ведёт бэкенд", **kw)
        _, theirs = await facts.propose(s, subject_kind="me", space_id=SPACE_ID_2,
                                        captured_in=SPACE_ID_2, text="Дача на мне", **kw)
        await s.commit()
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                       "query_string": b""})

    html = (await settings_router.profile_fact_verdict(
        request, SPACE_ID, rule.id, verdict="confirm"
    )).body.decode()
    # confirmed: under the editor with its kind and its letter; still proposed: Борис
    assert "— Счета в бухгалтерию" in html and "(правило;" in html
    assert f'hx-get="/email/{mail.id}" hx-target="#pane"' in html
    assert ">письмо от 11.09.2026<" in html
    assert "о Борис Иванов" in html and "«бэкенд и интеграции на тебе»" in html
    assert 'name="verdict" value="confirm"' in html and "Дача на мне" not in html

    html = (await settings_router.profile_fact_verdict(
        request, SPACE_ID, about.id, verdict="reject"
    )).body.decode()
    assert "Борис ведёт бэкенд" not in html
    # another space's fact is not this screen's to judge
    await settings_router.profile_fact_verdict(request, SPACE_ID, theirs.id, verdict="confirm")
    html = (await settings_router.profile_fact_verdict(
        request, SPACE_ID, rule.id, verdict="forget"
    )).body.decode()
    assert "Счета в бухгалтерию" not in html

    async with db() as s:
        rows = {r.text: r for r in (await s.execute(select(MemoryFact))).scalars()}
        assert rows["Счета в бухгалтерию"].status == "active"
        assert rows["Счета в бухгалтерию"].valid_to is not None
        assert rows["Борис ведёт бэкенд"].status == "rejected"
        assert rows["Дача на мне"].status == "proposed"
        assert await profiles.get_profile(s, SPACE_ID) == ""
