from __future__ import annotations

import uuid

from email_reader import prompts
from email_reader.ai.prompt import PROJECT_SYSTEM_PROMPT, SYSTEM_PROMPT


async def test_default_when_no_override(db):
    acc_a = uuid.uuid4()
    async with db() as s:
        assert await prompts.get_commitment_prompt(s, acc_a) == SYSTEM_PROMPT
        assert await prompts.get_project_prompt(s, acc_a) == PROJECT_SYSTEM_PROMPT
        # None account (unscoped) also falls back to default.
        assert await prompts.get_commitment_prompt(s, None) == SYSTEM_PROMPT


async def test_override_is_per_account(db):
    acc_a, acc_b = uuid.uuid4(), uuid.uuid4()
    async with db() as s:
        await prompts.set_prompt(s, "commitment", acc_a, "МОЙ промпт A")
        await s.commit()

        # A gets the built-in prompt WITH its addition; B the bare default (isolation).
        own = await prompts.get_commitment_prompt(s, acc_a)
        assert own.startswith(SYSTEM_PROMPT) and own.endswith("МОЙ промпт A")
        assert await prompts.get_commitment_prompt(s, acc_b) == SYSTEM_PROMPT
        # Overriding one prompt doesn't touch the other kind for the same account.
        assert await prompts.get_project_prompt(s, acc_a) == PROJECT_SYSTEM_PROMPT
        assert await prompts.get_override(s, "commitment", acc_a) == "МОЙ промпт A"

        # Reset restores the default and clears the override marker.
        await prompts.reset_prompt(s, "commitment", acc_a)
        await s.commit()
        assert await prompts.get_commitment_prompt(s, acc_a) == SYSTEM_PROMPT
        assert await prompts.get_override(s, "commitment", acc_a) is None


async def test_blank_override_falls_back_to_default(db):
    acc = uuid.uuid4()
    async with db() as s:
        await prompts.set_prompt(s, "commitment", acc, "   ")
        await s.commit()
        # Whitespace-only override is treated as "use default".
        assert await prompts.get_commitment_prompt(s, acc) == SYSTEM_PROMPT


async def test_an_addition_never_removes_the_built_in_rules(db):
    """«Суммаризируй встречу.» заменило весь промпт встреч пространства: модель разбирала
    встречи без типов, правил и названия. Теперь это дополнение к встроенному."""
    from email_reader.ai.prompt import MEETING_SYSTEM_PROMPT

    space = uuid.uuid4()
    async with db() as s:
        await prompts.set_prompt(s, "meeting", space, "Суммаризируй встречу.")
        got = await prompts.get_meeting_prompt(s, space)
    assert got.startswith(MEETING_SYSTEM_PROMPT)
    assert "record_meeting_routing" in got and got.endswith("Суммаризируй встречу.")


async def test_a_prompt_that_is_off_by_default_is_the_owners_text_alone(db):
    space = uuid.uuid4()
    async with db() as s:
        assert await prompts.get_mail_summary_prompt(s, space) == ""
        await prompts.set_prompt(s, "mail_summary", space, "СЖИМАЙ")
        assert await prompts.get_mail_summary_prompt(s, space) == "СЖИМАЙ"
