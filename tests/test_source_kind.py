"""source_kind / account_id on ActionItem: inferred defaults, direct mailbox ownership
inside the account filter, and the retype merge that must stay within a thread."""

from __future__ import annotations

from datetime import UTC, datetime

from email_reader import views
from email_reader.enums import ActionType, SourceKind
from email_reader.models import Account, ActionItem, Meeting, Thread
from tests.conftest import SPACE_ID, SPACE_ID_2, mk_space


async def test_source_kind_is_inferred_from_the_reference_that_is_set(db):
    async with db() as s:
        await mk_space(s)
        acc = Account(space_id=SPACE_ID, name="w", host="h", login="me@ya.ru", folders=[])
        s.add(acc)
        await s.flush()
        th = Thread(account_id=acc.id)
        m = Meeting(space_id=SPACE_ID, ext_id="m1", started_at=datetime.now(UTC))
        s.add_all([th, m])
        await s.flush()
        mail = ActionItem(
            space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="m", status="open",
                          source_thread_id=th.id)
        spoken = ActionItem(
            space_id=SPACE_ID, type=ActionType.DELEGATED_TASK, title="s", status="open",
                            source_meeting_id=m.id)
        typed = ActionItem(
            space_id=SPACE_ID, type=ActionType.MY_COMMITMENT, title="t", status="open")
        s.add_all([mail, spoken, typed])
        await s.commit()
        assert (mail.source_kind, spoken.source_kind, typed.source_kind) == (
            SourceKind.MAIL, SourceKind.MEETING, SourceKind.MANUAL,
        )


async def test_thread_less_item_is_scoped_by_its_space(db):
    """Rewritten for spaces: a task typed into space 1 must not show in space 2. It has no
    mailbox behind it, so the row's source map does not name one — the icon does."""
    async with db() as s:
        await mk_space(s)
        await mk_space(s, 2)
        ai = ActionItem(space_id=SPACE_ID, type=ActionType.MY_COMMITMENT, title="typed",
                        status="open", source_kind=SourceKind.MANUAL)
        s.add(ai)
        await s.commit()
        mine = await views.list_view(s, "commitments", space_id=SPACE_ID)
        assert [i.title for i in mine] == ["typed"]
        assert await views.list_view(s, "commitments", space_id=SPACE_ID_2) == []
        assert len(await views.list_view(s, "commitments")) == 1
        assert await views.sources_for_items(s, [ai]) == {}


async def test_retype_never_merges_thread_less_items(db):
    """`== None` renders as IS NULL: two meeting tasks with no counterparty would look like
    «the same» key and one would be deleted on retype. The merge only makes sense inside
    a thread."""
    async with db() as s:
        await mk_space(s)
        m = Meeting(space_id=SPACE_ID, ext_id="m1", started_at=datetime.now(UTC))
        s.add(m)
        await s.flush()
        one = ActionItem(
            space_id=SPACE_ID, type=ActionType.MY_COMMITMENT, title="one", status="open",
                         source_meeting_id=m.id)
        two = ActionItem(
            space_id=SPACE_ID, type=ActionType.DELEGATED_TASK, title="two", status="open",
                         source_meeting_id=m.id)
        s.add_all([one, two])
        await s.commit()
        await views.set_type(s, two.id, "MY_COMMITMENT")
        await s.commit()
        titles = sorted(i.title for i in await views.list_view(s, "commitments"))
        assert titles == ["one", "two"]  # both survive; nothing was «merged»
