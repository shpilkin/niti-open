"""Как Niti разговаривает со шлюзом модели: просьба не рассуждать и оборванный ответ.

Рассуждающая модель тратит лимит токенов сначала на размышления. Пока лимит не дотянут,
шлюз возвращает finish_reason=length с пустым ответом — раньше Niti записывал такой ответ
как «задач нет» и письмо молча теряло задачи.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from email_reader.ai import extractor
from email_reader.ai.errors import ReplyTruncated, explain
from email_reader.ai.schemas import TOOL_NAME

TODAY = date(2026, 9, 17)
THREAD = [{"is_me": False, "who": "Пётр", "sent_at": datetime(2026, 9, 16, tzinfo=UTC),
           "subject": "Счёт", "body": "Пришли счёт до пятницы."}]
ITEMS = {"items": [{"type": "MY_COMMITMENT", "title": "Прислать счёт", "confidence": 0.9}]}


def _reply(*, finish: str = "stop", content: str | None = None, tool_args: dict | None = None):
    calls = (
        [SimpleNamespace(function=SimpleNamespace(name=TOOL_NAME, arguments=json.dumps(tool_args)))]
        if tool_args is not None
        else None
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=finish, message=SimpleNamespace(content=content, tool_calls=calls)
        )],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
    )


def fake_openai(monkeypatch, *replies):
    """Подменяет OpenAI-клиент очередью ответов; возвращает список сделанных вызовов."""
    calls: list[dict] = []
    queue = list(replies)

    async def create(**kw):
        calls.append(kw)
        return queue.pop(0) if queue else replies[-1]

    def client(**_kw):
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(extractor, "AsyncOpenAI", client)
    return calls


def fake_anthropic(monkeypatch, resp):
    async def create(**_kw):
        return resp

    def client(**_kw):
        return SimpleNamespace(messages=SimpleNamespace(create=create))

    monkeypatch.setattr(extractor, "AsyncAnthropic", client)


NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False},
               "reasoning_effort": "none"}


async def test_a_connection_that_asks_for_no_thinking_says_so_in_every_call(monkeypatch):
    calls = fake_openai(monkeypatch, _reply(finish="tool_calls", tool_args=ITEMS))
    result, _ = await extractor.extract(
        THREAD, TODAY, kind="openai", api_key="k", model="m", no_thinking=True
    )
    assert [i.title for i in result.items] == ["Прислать счёт"]
    assert calls[0]["extra_body"] == NO_THINKING


async def test_a_connection_without_that_box_ticked_sends_nothing_extra(monkeypatch):
    calls = fake_openai(monkeypatch, _reply(finish="tool_calls", tool_args=ITEMS))
    await extractor.extract(THREAD, TODAY, kind="openai", api_key="k", model="m")
    assert "extra_body" not in calls[0]


async def test_an_answer_cut_off_by_the_limit_is_an_error_not_an_empty_result(monkeypatch):
    calls = fake_openai(monkeypatch, _reply(finish="length", content=""))
    with pytest.raises(ReplyTruncated):
        await extractor.extract(THREAD, TODAY, kind="openai", api_key="k", model="m")
    assert len(calls) == 1  # запасная попытка упёрлась бы в тот же лимит


async def test_a_gateway_without_tools_still_falls_back_to_json(monkeypatch):
    calls = fake_openai(
        monkeypatch,
        _reply(finish="stop", content="я не умею инструменты"),
        _reply(finish="stop", content=json.dumps(ITEMS)),
    )
    result, _ = await extractor.extract(THREAD, TODAY, kind="openai", api_key="k", model="m")
    assert [i.title for i in result.items] == ["Прислать счёт"] and len(calls) == 2


async def test_a_cut_off_memo_is_an_error_too(monkeypatch):
    fake_openai(monkeypatch, _reply(finish="length", content="Памятка к встрече: 1) обсуд"))
    with pytest.raises(ReplyTruncated):
        await extractor.complete_text(
            kind="openai", base_url=None, api_key="k", model="m",
            system="s", user="u", max_tokens=700,
        )


async def test_a_cut_off_answer_from_claude_is_an_error_as_well(monkeypatch):
    fake_anthropic(monkeypatch, SimpleNamespace(
        stop_reason="max_tokens", content=[],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    ))
    with pytest.raises(ReplyTruncated):
        await extractor.extract(THREAD, TODAY, kind="anthropic", api_key="k", model="claude")


async def test_the_connection_check_is_a_ping_and_survives_its_own_token_limit(monkeypatch):
    calls = fake_openai(monkeypatch, _reply(finish="length", content=""))
    err = await extractor.test_connection(
        kind="openai", base_url=None, api_key="k", model="m", no_thinking=True
    )
    assert err is None                               # обрыв на max_tokens=1 — ожидаемый
    assert calls[0]["extra_body"] == NO_THINKING     # шлюз проверяется теми же параметрами


def test_a_truncated_reply_is_explained_by_the_token_limit():
    said = explain(repr(ReplyTruncated("ответ модели оборван: finish_reason=length, "
                                       "max_tokens=2048")))
    assert "лимит токенов" in said and "без рассуждений" in said


async def test_a_connection_remembers_that_it_answers_without_thinking(db):
    from email_reader import model_connections as mc

    async with db() as s:
        conn = await mc.create_connection(
            s, name="Локальная", provider="openai_custom", base_url="http://127.0.0.1:8080/v1",
            api_key="local", model="local-model", no_thinking=True,
        )
        await s.commit()
        assert mc.resolve_connection(conn).no_thinking is True
        assert (await mc.resolve_active(s)).no_thinking is True

        await mc.update_connection(s, conn.id, no_thinking=False)
        await s.commit()
        assert (await mc.resolve_active(s)).no_thinking is False
