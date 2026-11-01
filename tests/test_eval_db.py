"""The eval set grows with the user's corrections: eval_samples become cases."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from email_reader.enums import Direction
from email_reader.eval.scorer import load_db_samples
from email_reader.models import Account, Email, EvalSample, Thread
from tests.conftest import SPACE_ID, mk_space

T0 = datetime(2026, 8, 30, 10, tzinfo=UTC)


async def test_latest_sample_per_thread_rendered_as_the_extractor_sees_it(db):
    async with db() as s:
        await mk_space(s)
        a = Account(space_id=SPACE_ID, name="acc", host="imap.b.ru", port=993, login="me@b.ru")
        s.add(a)
        await s.flush()
        t = Thread(account_id=a.id)
        s.add(t)
        await s.flush()
        s.add(Email(
            account_id=a.id, thread_id=t.id, dedup_hash=uuid.uuid4().hex,
            direction=Direction.INBOUND, from_addr="anna@x.ru", from_name="Анна",
            to_addrs=[], cc_addrs=[], sent_at=T0, subject="Сроки", body_text="Когда?",
        ))
        s.add(Email(
            account_id=a.id, thread_id=t.id, dedup_hash=uuid.uuid4().hex,
            direction=Direction.OUTBOUND, from_addr="me@b.ru",
            to_addrs=[], cc_addrs=[], sent_at=T0 + timedelta(hours=1), body_text="Завтра",
        ))
        s.add(EvalSample(
            thread_id=t.id, created_at=datetime(2026, 9, 1, tzinfo=UTC),
            expected={"items": [{"type": "NEED_MY_REPLY", "counterparty": "anna@x.ru"}]},
        ))
        s.add(EvalSample(
            thread_id=t.id, created_at=datetime(2026, 9, 3, tzinfo=UTC),
            expected={"items": [{"type": "WAITING_FOR_REPLY", "counterparty": "anna@x.ru"}]},
        ))
        s.add(EvalSample(thread_id=None, expected={"items": []}))  # purged thread
        await s.commit()
        cases = await load_db_samples(s)
        bare = await load_db_samples(s, memory=False)

    assert len(cases) == 1
    case = cases[0]
    # Scored with the prompt the extractor gets in production: memory block included…
    assert "ПАМЯТЬ (справочные данные" in case.system_prompt
    assert "КОНТРАГЕНТ: Анна <anna@x.ru>" in case.system_prompt
    # …and comparable without it.
    assert "ПАМЯТЬ (справочные данные" not in bare[0].system_prompt
    assert case.expected == [{"type": "WAITING_FOR_REPLY", "counterparty": "anna@x.ru"}]
    assert case.today == date(2026, 9, 3)
    assert [m["is_me"] for m in case.messages] == [False, True]
    assert case.messages[0]["who"] == "Анна"
    assert case.messages[0]["body"] == "Когда?"


async def test_no_samples_no_cases(db):
    async with db() as s:
        assert await load_db_samples(s) == []
