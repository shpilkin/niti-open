"""LLM extraction — two backends, one structured result.

- Anthropic (native SDK): forced tool-call (most reliable for Claude).
- OpenAI-compatible (OpenRouter / RouterAI / LiteLLM / custom): forced function
  calling, with a JSON-in-prompt fallback for models without reliable tool support.

The caller passes a resolved connection (kind/base_url/api_key/model); changes in the UI
take effect without a restart.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel

from email_reader.ai.errors import ReplyTruncated
from email_reader.ai.prompt import (
    MEETING_SUMMARY_SYSTEM,
    MEETING_SYSTEM_PROMPT,
    PROJECT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    render_meeting,
    render_projects,
    render_thread,
)
from email_reader.ai.schemas import (
    MEETING_TOOL_NAME,
    MEETING_TOOL_SCHEMA,
    PROJECT_TOOL_NAME,
    PROJECT_TOOL_SCHEMA,
    TOOL_NAME,
    TOOL_SCHEMA,
    ExtractionResult,
    MeetingRoutingResult,
    ProjectMatchResult,
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# OpenAI-style function schema derived from the Anthropic tool schema.
_OPENAI_TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": TOOL_SCHEMA["description"],
        "parameters": TOOL_SCHEMA["input_schema"],
    },
}

_OPENAI_PROJECT_TOOL = {
    "type": "function",
    "function": {
        "name": PROJECT_TOOL_NAME,
        "description": PROJECT_TOOL_SCHEMA["description"],
        "parameters": PROJECT_TOOL_SCHEMA["input_schema"],
    },
}

_OPENAI_MEETING_TOOL = {
    "type": "function",
    "function": {
        "name": MEETING_TOOL_NAME,
        "description": MEETING_TOOL_SCHEMA["description"],
        "parameters": MEETING_TOOL_SCHEMA["input_schema"],
    },
}


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


# Рассуждающая модель тратит max_tokens сначала на размышления; шлюзы отключают их
# по-разному: llama.cpp — через шаблон чата (tools/server/README.md,
# "chat_template_kwargs"), splash и OpenRouter — параметром reasoning_effort
# (server/frontend.py; у OpenRouter проверено на deepseek-v4.1-flash 27.09.2026 —
# reasoning_tokens=0). Посылаем оба: каждый шлюз читает своё, чужое поле игнорирует.
_NO_THINKING = {
    "extra_body": {
        "chat_template_kwargs": {"enable_thinking": False},
        "reasoning_effort": "none",
    }
}


def _openai_opts(no_thinking: bool) -> dict:
    return dict(_NO_THINKING) if no_thinking else {}


def _openai_message(resp, max_tokens: int):
    """Ответ модели, или ReplyTruncated — если она не договорила в пределах лимита.

    Оборванный ответ нельзя разбирать: у tool-call обрезаны аргументы, у текста — конец.
    Раньше такой ответ становился пустым результатом («задач нет»)."""
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        raise ReplyTruncated(f"ответ модели оборван: finish_reason=length, "
                             f"max_tokens={max_tokens}")
    return choice.message


def _anthropic_content(resp, max_tokens: int) -> list:
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise ReplyTruncated(f"ответ модели оборван: stop_reason=max_tokens, "
                             f"max_tokens={max_tokens}")
    return resp.content


def _require_key(api_key: str | None) -> str:
    if not api_key:
        raise RuntimeError("API-ключ не задан (Настройки → AI / извлечение)")
    return api_key


def _loose_json(text: str | None) -> dict | None:
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def loose_json(text: str | None) -> dict | None:
    """The first JSON object in a model's reply, or None — for one-shot structured calls
    that go through complete_text (no tool support needed)."""
    return _loose_json(text)


async def _extract_anthropic(
    rendered: str, api_key: str, model: str, system: str
) -> tuple[ExtractionResult, Usage]:
    client = AsyncAnthropic(api_key=_require_key(api_key))
    resp = await client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": rendered}],
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": TOOL_NAME},
    )
    usage = Usage(getattr(resp.usage, "input_tokens", 0), getattr(resp.usage, "output_tokens", 0))
    for block in _anthropic_content(resp, 2048):
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return ExtractionResult.model_validate(block.input), usage
    return ExtractionResult(items=[]), usage


def _usage_openai(resp) -> Usage:
    u = getattr(resp, "usage", None)
    return Usage(getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)


async def _extract_openai(
    rendered: str, base_url: str | None, api_key: str, model: str, system: str,
    no_thinking: bool,
) -> tuple[ExtractionResult, Usage]:
    client = AsyncOpenAI(api_key=_require_key(api_key), base_url=base_url or None)
    base_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": rendered},
    ]
    # 1) Preferred: forced function calling.
    try:
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=2048,
            messages=base_messages,
            tools=[_OPENAI_TOOL],
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            **_openai_opts(no_thinking),
        )
        msg = _openai_message(resp, 2048)
        if msg.tool_calls:
            args = json.loads(msg.tool_calls[0].function.arguments)
            return ExtractionResult.model_validate(args), _usage_openai(resp)
        parsed = _loose_json(msg.content)
        if parsed is not None:
            return ExtractionResult.model_validate(parsed), _usage_openai(resp)
    except ReplyTruncated:
        raise  # тот же лимит оборвёт и запасную попытку — не тратим её
    except Exception:  # noqa: BLE001 — provider lacks/forbids tools -> JSON fallback
        pass

    # 2) Fallback: ask for raw JSON and parse it.
    resp = await client.chat.completions.create(
        model=model,
        max_tokens=2048,
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": rendered
                + "\n\nВерни ТОЛЬКО JSON вида "
                '{"items": [{"type": ..., "title": ..., "counterparty": ..., '
                '"due_date": ..., "confidence": ..., "reason": ...}]}. Без пояснений.',
            },
        ],
        **_openai_opts(no_thinking),
    )
    parsed = _loose_json(_openai_message(resp, 2048).content) or {"items": []}
    return ExtractionResult.model_validate(parsed), _usage_openai(resp)


async def extract(
    messages: list[dict],
    today: date,
    *,
    kind: str = "anthropic",
    base_url: str | None = None,
    api_key: str | None = None,
    model: str,
    system_prompt: str | None = None,
    no_thinking: bool = False,
) -> tuple[ExtractionResult, Usage]:
    rendered = render_thread(messages, today)
    system = system_prompt or SYSTEM_PROMPT
    if kind == "openai":
        return await _extract_openai(
            rendered, base_url, api_key or "", model, system, no_thinking
        )
    return await _extract_anthropic(rendered, api_key or "", model, system)


async def _classify_anthropic(
    user: str, api_key: str, model: str, system: str
) -> tuple[ProjectMatchResult, Usage]:
    client = AsyncAnthropic(api_key=_require_key(api_key))
    resp = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[PROJECT_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": PROJECT_TOOL_NAME},
    )
    usage = Usage(getattr(resp.usage, "input_tokens", 0), getattr(resp.usage, "output_tokens", 0))
    for block in _anthropic_content(resp, 1024):
        if getattr(block, "type", None) == "tool_use" and block.name == PROJECT_TOOL_NAME:
            return ProjectMatchResult.model_validate(block.input), usage
    return ProjectMatchResult(matches=[]), usage


async def _classify_openai(
    user: str, base_url: str | None, api_key: str, model: str, system: str,
    no_thinking: bool,
) -> tuple[ProjectMatchResult, Usage]:
    client = AsyncOpenAI(api_key=_require_key(api_key), base_url=base_url or None)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=1024,
            messages=messages,
            tools=[_OPENAI_PROJECT_TOOL],
            tool_choice={"type": "function", "function": {"name": PROJECT_TOOL_NAME}},
            **_openai_opts(no_thinking),
        )
        msg = _openai_message(resp, 1024)
        if msg.tool_calls:
            args = json.loads(msg.tool_calls[0].function.arguments)
            return ProjectMatchResult.model_validate(args), _usage_openai(resp)
        parsed = _loose_json(msg.content)
        if parsed is not None:
            return ProjectMatchResult.model_validate(parsed), _usage_openai(resp)
    except ReplyTruncated:
        raise  # тот же лимит оборвёт и запасную попытку — не тратим её
    except Exception:  # noqa: BLE001 — provider lacks/forbids tools -> JSON fallback
        pass

    resp = await client.chat.completions.create(
        model=model,
        max_tokens=1024,
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": user
                + '\n\nВерни ТОЛЬКО JSON вида {"matches": [{"project": ..., "member": '
                'true|false, "confidence": ..., "reason": ...}]}. Без пояснений.',
            },
        ],
        **_openai_opts(no_thinking),
    )
    parsed = _loose_json(_openai_message(resp, 1024).content) or {"matches": []}
    return ProjectMatchResult.model_validate(parsed), _usage_openai(resp)


async def classify_projects(
    messages: list[dict],
    today: date,
    projects: list[dict],
    *,
    kind: str = "anthropic",
    base_url: str | None = None,
    api_key: str | None = None,
    model: str,
    system_prompt: str | None = None,
    no_thinking: bool = False,
) -> tuple[ProjectMatchResult, Usage]:
    """Decide which of `projects` ([{name, prompt}]) the thread belongs to."""
    user = render_thread(messages, today) + "\n" + render_projects(projects)
    system = system_prompt or PROJECT_SYSTEM_PROMPT
    if kind == "openai":
        return await _classify_openai(
            user, base_url, api_key or "", model, system, no_thinking
        )
    return await _classify_anthropic(user, api_key or "", model, system)


async def test_connection(
    *, kind: str, base_url: str | None, api_key: str | None, model: str,
    no_thinking: bool = False,
) -> str | None:
    """Lightweight liveness check. None = ok, else error string.

    max_tokens=1 — намеренный пинг, обрыв ответа тут ожидаем и ошибкой не считается.
    Зато шлюз, который не понимает chat_template_kwargs, ответит здесь ошибкой — для
    этого проверка и посылает те же параметры, что настоящий вызов."""
    try:
        key = _require_key(api_key)
    except RuntimeError as exc:
        return str(exc)
    try:
        if kind == "openai":
            client = AsyncOpenAI(api_key=key, base_url=base_url or None)
            await client.chat.completions.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
                **_openai_opts(no_thinking),
            )
        else:
            client = AsyncAnthropic(api_key=key)
            await client.models.retrieve(model)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


async def complete_text(
    *,
    kind: str,
    base_url: str | None,
    api_key: str | None,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 700,
    no_thinking: bool = False,
) -> str | None:
    """Plain-text one-shot completion over either backend (no tools).

    Shared plumbing for free-text features (e.g. meeting-prep memos). Raises on
    transport/auth errors — callers decide whether that is fatal or best-effort.
    Returns None only when the model answers with empty content."""
    key = _require_key(api_key)
    if kind == "openai":
        client = AsyncOpenAI(api_key=key, base_url=base_url or None)
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **_openai_opts(no_thinking),
        )
        return (_openai_message(resp, max_tokens).content or "").strip() or None
    client = AsyncAnthropic(api_key=key)
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        b.text for b in _anthropic_content(resp, max_tokens) if getattr(b, "type", "") == "text"
    ).strip()
    return text or None


# --- Meetings: fold the transcript, then route the fold ---

# A 1–2 hour meeting is 60–100k characters of unedited ASR. Feeding that to the router
# both drowns the model and costs real money, so the transcript is folded first (§6 of
# docs/feature-routing.md): N cheap summarization calls, then ONE routing call over the
# fold. Chunks are cut on line boundaries so a sentence isn't split across calls.
_FOLD_CHUNK_CHARS = 24_000
_FOLD_MAX_CHUNKS = 12


def split_transcript(text: str, chunk_chars: int = _FOLD_CHUNK_CHARS) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in (text or "").splitlines(keepends=True):
        if size + len(line) > chunk_chars and current:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks[:_FOLD_MAX_CHUNKS]


async def fold_transcript(
    text: str,
    *,
    kind: str = "anthropic",
    base_url: str | None = None,
    api_key: str | None = None,
    model: str,
    system: str | None = None,
    no_thinking: bool = False,
) -> tuple[str, int]:
    """Compress a raw transcript into a digest for routing. Returns (fold, calls made).

    A short transcript is its own best fold — we don't spend a call to shorten what
    already fits."""
    text = (text or "").strip()
    if not text:
        return "", 0
    chunks = split_transcript(text)
    if len(chunks) <= 1 and len(text) <= _FOLD_CHUNK_CHARS // 2:
        return text, 0
    system = system or MEETING_SUMMARY_SYSTEM
    parts: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        head = f"Часть {i} из {len(chunks)}." if len(chunks) > 1 else ""
        out = await complete_text(
            kind=kind, base_url=base_url, api_key=api_key, model=model,
            # Рассуждающая модель (glm-5.3-flash на OpenRouter — рассуждения не отключаются)
            # тратила ~3000 токенов на обдумывание куска, и при 2500 ответ был пустым.
            system=system, user=f"{head}\n\n{chunk}", max_tokens=6000,
            no_thinking=no_thinking,
        )
        if out:
            parts.append(out)
    if not parts:
        # Every fold call came back empty: fall back to the raw head rather than
        # routing on nothing.
        return text[:_FOLD_CHUNK_CHARS], len(chunks)
    return "\n\n".join(parts), len(chunks)


async def _route_meeting_anthropic(
    user: str, api_key: str, model: str, system: str
) -> tuple[MeetingRoutingResult, Usage]:
    client = AsyncAnthropic(api_key=_require_key(api_key))
    resp = await client.messages.create(
        model=model,
        max_tokens=6000,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[MEETING_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": MEETING_TOOL_NAME},
    )
    usage = Usage(getattr(resp.usage, "input_tokens", 0), getattr(resp.usage, "output_tokens", 0))
    for block in _anthropic_content(resp, 6000):
        if getattr(block, "type", None) == "tool_use" and block.name == MEETING_TOOL_NAME:
            return MeetingRoutingResult.model_validate(block.input), usage
    return MeetingRoutingResult(), usage


async def _route_meeting_openai(
    user: str, base_url: str | None, api_key: str, model: str, system: str,
    no_thinking: bool,
) -> tuple[MeetingRoutingResult, Usage]:
    client = AsyncOpenAI(api_key=_require_key(api_key), base_url=base_url or None)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=6000,
            messages=messages,
            tools=[_OPENAI_MEETING_TOOL],
            tool_choice={"type": "function", "function": {"name": MEETING_TOOL_NAME}},
            **_openai_opts(no_thinking),
        )
        msg = _openai_message(resp, 6000)
        if msg.tool_calls:
            args = json.loads(msg.tool_calls[0].function.arguments)
            return MeetingRoutingResult.model_validate(args), _usage_openai(resp)
        parsed = _loose_json(msg.content)
        if parsed is not None:
            return MeetingRoutingResult.model_validate(parsed), _usage_openai(resp)
    except ReplyTruncated:
        raise  # тот же лимит оборвёт и запасную попытку — не тратим её
    except Exception:  # noqa: BLE001 — provider lacks/forbids tools -> JSON fallback
        pass

    resp = await client.chat.completions.create(
        model=model,
        max_tokens=6000,
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": user
                + '\n\nВерни ТОЛЬКО JSON вида {"title": ..., "about": ..., "discussed": '
                '[...], "agreed": [...], "open_questions": [...], "items": '
                '[{"type": ..., "title": ..., "counterparty": ..., "due_date": ..., '
                '"at": ..., "project_hint": ..., "confidence": ...}], "matches": '
                '[{"project": ..., "member": true|false, "confidence": ..., "reason": ..., '
                '"digest": ...}]}. Без пояснений.',
            },
        ],
        **_openai_opts(no_thinking),
    )
    parsed = _loose_json(_openai_message(resp, 6000).content) or {}
    return MeetingRoutingResult.model_validate(parsed), _usage_openai(resp)


# Один вызов на всё проигрывал задачи: подробные digest по каждому проекту съедали ответ,
# и договорённости набирались только из первых минут встречи (27.09.2026).
_FOCUS = {
    "items": (
        "\n\nЭТОТ ВЫЗОВ: заполни title, about, discussed, agreed, open_questions, "
        "participants и items — по ВСЕЙ встрече, от начала до конца, не только по первым "
        "темам. matches оставь пустым: раскладка по проектам делается отдельным вызовом."
    ),
    "matches": (
        "\n\nЭТОТ ВЫЗОВ: заполни только matches (с digest для принадлежащих проектов). "
        "title, about, discussed, agreed, open_questions и items оставь пустыми — они "
        "сделаны отдельным вызовом."
    ),
}


async def route_meeting(
    fold: str,
    projects: list[dict],
    today: date,
    hints: list[str] | None = None,
    *,
    kind: str = "anthropic",
    base_url: str | None = None,
    api_key: str | None = None,
    model: str,
    system_prompt: str | None = None,
    no_thinking: bool = False,
    focus: str | None = None,
) -> tuple[MeetingRoutingResult, Usage]:
    """Summary + items + per-project verdicts for one meeting. `focus` splits the job in
    two calls (meetings.py): "items" — title, summary and agreements over the whole
    meeting; "matches" — only the per-project verdicts. None — everything at once."""
    user = render_meeting(fold, projects, today, hints) + _FOCUS.get(focus or "", "")
    system = system_prompt or MEETING_SYSTEM_PROMPT
    if kind == "openai":
        return await _route_meeting_openai(
            user, base_url, api_key or "", model, system, no_thinking
        )
    return await _route_meeting_anthropic(user, api_key or "", model, system)


async def structured_call(
    *,
    kind: str,
    base_url: str | None,
    api_key: str | None,
    model: str,
    system: str,
    user: str,
    tool_schema: dict,
    result_model: type[BaseModel],
    max_tokens: int = 1024,
    fallback_hint: str = "",
    no_thinking: bool = False,
):
    """One forced tool call whose result is `result_model` — for a caller that brings its
    own schema (advice). The same shape as the extractor's and the router's calls:
    Anthropic forces the tool; an OpenAI-compatible gateway gets forced function
    calling, then JSON-in-prompt when it has no tools. Returns (result, Usage)."""
    name = tool_schema["name"]
    if kind == "openai":
        client = AsyncOpenAI(api_key=_require_key(api_key), base_url=base_url or None)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            resp = await client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
                tools=[{
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool_schema.get("description", ""),
                        "parameters": tool_schema["input_schema"],
                    },
                }],
                tool_choice={"type": "function", "function": {"name": name}},
                **_openai_opts(no_thinking),
            )
            msg = _openai_message(resp, max_tokens)
            if msg.tool_calls:
                args = json.loads(msg.tool_calls[0].function.arguments)
                return result_model.model_validate(args), _usage_openai(resp)
            parsed = _loose_json(msg.content)
            if parsed is not None:
                return result_model.model_validate(parsed), _usage_openai(resp)
        except ReplyTruncated:
            raise  # тот же лимит оборвёт и запасную попытку — не тратим её
        except Exception:  # noqa: BLE001 — provider lacks/forbids tools -> JSON fallback
            pass
        hint = f" вида {fallback_hint}" if fallback_hint else ""
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"{user}\n\nВерни ТОЛЬКО JSON{hint}. Без пояснений."},
            ],
            **_openai_opts(no_thinking),
        )
        parsed = _loose_json(_openai_message(resp, max_tokens).content) or {}
        return result_model.model_validate(parsed), _usage_openai(resp)

    client = AsyncAnthropic(api_key=_require_key(api_key))
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool_schema],
        tool_choice={"type": "tool", "name": name},
    )
    usage = Usage(getattr(resp.usage, "input_tokens", 0), getattr(resp.usage, "output_tokens", 0))
    for block in _anthropic_content(resp, max_tokens):
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return result_model.model_validate(block.input), usage
    return result_model(), usage
