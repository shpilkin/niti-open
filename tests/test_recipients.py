"""Recipient expander: get_thread_messages carries full To/CC lists, and the shared
`recipients` macro truncates with an inline-revealable remainder."""

from __future__ import annotations

from datetime import UTC, datetime

from jinja2 import Environment, FileSystemLoader

from email_reader import views
from email_reader.enums import ActionStatus, ActionType
from email_reader.models import Account, ActionItem, Email, Thread
from email_reader.paths import resource_path
from tests.conftest import SPACE_ID, mk_space

_ENV = Environment(loader=FileSystemLoader(str(resource_path("templates"))), autoescape=True)


def _render_recipients(label, addrs, limit=2):
    tpl = _ENV.from_string(
        "{% import '_macros.html' as m %}{{ m.recipients(label, addrs, limit) }}"
    )
    return tpl.render(label=label, addrs=addrs, limit=limit)


def test_macro_truncates_and_keeps_full_list():
    addrs = [{"email": f"u{i}@x.ru", "name": f"U{i}"} for i in range(7)]
    html = _render_recipients("Кому", addrs)
    assert "U0, U1" in html  # head (limit=2)
    assert "+5" in html  # remainder count
    assert "U6" in html  # full list present in the DOM (revealed on click)
    assert 'class="recip"' in html and "<details" in html


def test_macro_no_expander_when_short():
    html = _render_recipients("Кому", [{"email": "solo@x.ru", "name": "Solo"}])
    assert "Solo" in html
    assert "<details" not in html  # nothing to expand
    assert "+" not in html


def test_macro_empty():
    assert _render_recipients("Кому", []).strip() == ""


async def test_thread_messages_carry_full_recipient_lists(db):
    to = [{"email": f"u{i}@x.ru", "name": f"U{i}"} for i in range(4)]
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@ya.ru", folders=[])
        s.add(acc)
        await s.flush()
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()
        s.add(Email(
            account_id=acc.id, thread_id=th.id, direction="inbound", from_addr="a@x.ru",
            to_addrs=to, cc_addrs=[{"email": "c@x.ru", "name": "C"}], dedup_hash="d1",
            sent_at=datetime.now(UTC), subject="s", body_text="b",
        ))
        await s.commit()
        msgs = await views.get_thread_messages(s, th.id)
    assert len(msgs) == 1
    assert msgs[0].to_addrs == to  # full list, not a pre-truncated string
    assert msgs[0].cc_addrs == [{"email": "c@x.ru", "name": "C"}]


async def test_person_dossier_still_works(db):
    """Sanity: the counterparty dossier query is unaffected by the ThreadMessage change."""
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@ya.ru", folders=[])
        s.add(acc)
        await s.flush()
        th = Thread(account_id=acc.id)
        s.add(th)
        await s.flush()
        s.add(ActionItem(space_id=SPACE_ID, 
            type=ActionType.NEED_MY_REPLY, title="t", status=ActionStatus.OPEN,
            source_thread_id=th.id, counterparty="Пётр",
        ))
        await s.commit()
        d = await views.person_dossier(s, "Пётр", SPACE_ID)
    assert len(d["items"]) == 1
