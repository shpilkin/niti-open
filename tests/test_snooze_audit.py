"""Deferring a card is a reaction worth remembering: snooze lands in agent_decisions."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from email_reader import views
from email_reader.enums import ActionStatus, ActionType
from email_reader.models import ActionItem, AgentDecision
from tests.conftest import SPACE_ID, mk_space


async def test_snooze_journals_a_user_action(db):
    async with db() as s:
        await mk_space(s)
        ai = ActionItem(
            space_id=SPACE_ID, type=ActionType.NEED_MY_REPLY, title="Ответить",
            status=ActionStatus.OPEN,
        )
        s.add(ai)
        await s.commit()
        await views.snooze(s, ai.id, days=3)
        await s.commit()
        decisions = (
            await s.execute(select(AgentDecision).where(AgentDecision.action_item_id == ai.id))
        ).scalars().all()

    assert [d.kind for d in decisions] == ["user_action"]
    assert decisions[0].payload["action"] == "snooze"
    assert decisions[0].payload["until"] == (views._today() + timedelta(days=3)).isoformat()
