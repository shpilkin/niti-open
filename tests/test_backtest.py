"""Priority backtest on self-labels: the label is «I wrote back within the window», the
features are only what was known before the message arrived."""

from __future__ import annotations

import uuid
from collections import namedtuple
from datetime import UTC, datetime, timedelta

from email_reader.enums import Direction
from email_reader.memory import backtest
from email_reader.memory.backtest import auc, build_samples, score_baseline, score_memory
from email_reader.models import Account, Email, Identity, Thread
from tests.conftest import SPACE_ID, mk_space

ME = "me@b.ru"
A = "a@x.ru"
B = "b@x.ru"
NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
D = timedelta(days=1)
H = timedelta(hours=1)

Row = namedtuple(
    "Row", "id thread_id direction from_addr to_addrs cc_addrs sent_at is_low_value question"
)


def _addrs(*addrs):
    return [{"email": a, "name": None} for a in addrs]


def inbound(addr, thread, at, *, to=(ME,), cc=(), low=False, q=False):
    return Row(uuid.uuid4(), thread, Direction.INBOUND, addr, _addrs(*to), _addrs(*cc), at, low, q)


def outbound(thread, at, to=(A,)):
    return Row(uuid.uuid4(), thread, Direction.OUTBOUND, ME, _addrs(*to), [], at, False, False)


def test_labels_and_sender_history_without_leakage():
    rows = []
    # A: three earlier messages, two answered within hours; the third answered only AFTER
    # the sample below — it must not count as answered for that sample.
    for i, days in enumerate((30, 20, 5)):
        t = uuid.uuid4()
        rows.append(inbound(A, t, NOW - days * D))
        rows.append(outbound(t, NOW - days * D + (2 * H if i < 2 else 3 * D)))
    # B: three earlier messages, never answered.
    for days in (30, 20, 10):
        rows.append(inbound(B, uuid.uuid4(), NOW - days * D))
    ta, tb = uuid.uuid4(), uuid.uuid4()
    rows.append(inbound(A, ta, NOW - 3 * D, q=True))
    rows.append(outbound(ta, NOW - 3 * D + 1 * H))
    rows.append(inbound(B, tb, NOW - 3 * D))

    samples = build_samples(rows, {ME}, [], now=NOW, window_hours=24)
    by_addr = {s.addr: s for s in samples if s.sent_at == NOW - 3 * D}
    sa, sb = by_addr[A], by_addr[B]
    assert sa.label and not sb.label
    assert sa.features["reply_rate"] == 2 / 3
    assert sa.features["fast"] == 1.0 and sa.features["question"] == 1.0
    assert sb.features["reply_rate"] == 0.0 and sb.features["familiar"] == 0.3
    assert score_memory(sa) > score_memory(sb)
    labels = [s.label for s in samples]
    # The history messages are samples too, and for them nothing was known yet — so the
    # ranking over everything is not perfect, but it must beat the one-bit board order.
    assert auc([score_baseline(s) for s in samples], labels) == 0.5  # nobody is VIP
    assert auc([score_memory(s) for s in samples], labels) > 0.75


def test_open_window_cc_only_low_value_and_self_are_not_samples():
    t = uuid.uuid4()
    rows = [
        inbound(A, t, NOW - 1 * H),                       # window still open
        inbound(A, uuid.uuid4(), NOW - 5 * D, to=("x@y.ru",), cc=(ME,)),  # CC only
        inbound(A, uuid.uuid4(), NOW - 5 * D, low=True),  # newsletter
        Row(uuid.uuid4(), uuid.uuid4(), Direction.SELF, ME, _addrs(ME), [], NOW - 5 * D,
            False, False),
        inbound(A, uuid.uuid4(), NOW - 6 * D),            # the one real sample
    ]
    samples = build_samples(rows, {ME}, [], now=NOW, window_hours=24)
    assert [s.sent_at for s in samples] == [NOW - 6 * D]
    assert samples[0].features["ongoing"] == 0.0 and samples[0].features["direct"] == 1.0


def test_vip_and_ongoing_conversation_are_seen():
    t = uuid.uuid4()
    rows = [outbound(t, NOW - 10 * D), inbound(A, t, NOW - 9 * D, to=(ME, B))]
    entries = [{"type": "domain", "pattern": "x.ru"}]
    s = build_samples(rows, {ME}, entries, now=NOW, window_hours=24)[0]
    assert s.features["vip"] == 1.0 and s.features["ongoing"] == 1.0 and s.features["direct"] == 0.0


def test_auc_ties_and_degenerate_inputs():
    assert auc([1, 1, 0, 0], [True, False, True, False]) == 0.5
    assert auc([0.9, 0.1], [True, False]) == 1.0
    assert auc([0.1, 0.9], [True, False]) == 0.0
    assert auc([1, 0], [True, True]) is None


def test_report_shows_both_orders_and_every_feature():
    t = uuid.uuid4()
    rows = [inbound(A, t, NOW - 5 * D), outbound(t, NOW - 5 * D + 1 * H),
            inbound(B, uuid.uuid4(), NOW - 5 * D)]
    text = backtest.report(build_samples(rows, {ME}, [], now=NOW, window_hours=24), 24)
    assert "Писем мне: 2, из них отвечено за 24 ч: 1 (50%)." in text
    assert "доска сегодня (VIP)" in text and "VIP + история" in text
    assert "только история" in text and "Случайный порядок: AUC 0.500" in text
    for f in backtest.FEATURES:
        assert f"  {f}" in text
    assert backtest.report([], 24).startswith("Нет входящих")


async def test_run_space_reads_the_space_only(db):
    async with db() as s:
        await mk_space(s, 1)
        await mk_space(s, 2)
        work = Account(space_id=SPACE_ID, name="w", host="h", port=993, login=ME)
        home = Account(space_id=uuid.UUID(int=2), name="p", host="h", port=993, login="p@b.ru")
        s.add_all([work, home, Identity(email=ME)])
        await s.flush()
        tw = Thread(account_id=work.id)
        th = Thread(account_id=home.id)
        s.add_all([tw, th])
        await s.flush()

        def mail(acc, thread, direction, frm, to, at):
            return Email(account_id=acc.id, thread_id=thread.id, dedup_hash=uuid.uuid4().hex,
                         direction=direction, from_addr=frm, to_addrs=_addrs(to), cc_addrs=[],
                         sent_at=at, subject="Вопрос?")

        s.add_all([
            mail(work, tw, Direction.INBOUND, A, ME, NOW - 5 * D),
            mail(work, tw, Direction.OUTBOUND, ME, A, NOW - 5 * D + 2 * H),
            mail(home, th, Direction.INBOUND, B, ME, NOW - 5 * D),
        ])
        await s.commit()
        text = await backtest.run_space(s, SPACE_ID, days=30, window_hours=24, now=NOW)
    assert "Писем мне: 1, из них отвечено за 24 ч: 1 (100%)." in text


def test_holdout_picks_on_the_first_half_and_judges_on_the_second():
    rows = []
    for k in range(30):  # 30 threads with A: always answered; 30 with B: never
        ta, tb = uuid.uuid4(), uuid.uuid4()
        at = NOW - (90 - k * 2) * D
        rows.append(inbound(A, ta, at))
        rows.append(outbound(ta, at + 1 * H))
        rows.append(inbound(B, tb, at))
    samples = build_samples(rows, {ME}, [], now=NOW, window_hours=24)
    text = backtest.holdout_report(samples, 24)
    assert text.startswith("Холдаут: выбор на первой половине")
    assert "← выбран на первой половине" in text
    assert "только история" in text
    assert backtest.holdout_report(samples[:10], 24).startswith("Холдаут: слишком мало")
