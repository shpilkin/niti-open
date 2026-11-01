"""Extraction output schema (validated) + the tool schema sent to the model."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, field_validator, model_validator

from email_reader.enums import ActionType

# Types the MAIL extractor may emit. Deliberately not `[t for t in ActionType]`: that list
# also carries types produced by other sources (MEETING_PREP, now only in the archive; DECISION
# by the meeting router), and offering them here invites the model to use them on mail.
MAIL_ACTION_TYPES = [
    ActionType.MY_COMMITMENT,
    ActionType.DELEGATED_TASK,
    ActionType.TO_DELEGATE,
    ActionType.WAITING_FOR_REPLY,
    ActionType.NEED_MY_REPLY,
    ActionType.INFO,
]


# A counterparty is a name or an address on one line. The model has been seen writing
# its whole chain of thought into the field (4 500 characters); the first-line rule and
# the cap keep the column a name. Applied at validation, so every entrance is covered.
CP_LIMIT = 120


def one_line(v, limit: int = CP_LIMIT):
    """None for nothing («», «null», blank); else the first non-empty line, at most
    `limit` characters. Non-strings pass through for pydantic to reject."""
    if v is None or not isinstance(v, str):
        return v
    lines = [ln.strip() for ln in v.splitlines()]
    lines = [ln for ln in lines if ln and ln != "null"]
    if not lines:
        return None
    return lines[0][:limit].rstrip() or None


class ExtractedItem(BaseModel):
    type: ActionType
    title: str
    counterparty: str | None = None
    due_date: date | None = None
    delegate_to: str | None = None  # TO_DELEGATE: suggested team/person from the profile
    confidence: float = 0.5
    reason: str | None = None

    @field_validator("due_date", mode="before")
    @classmethod
    def _empty_date(cls, v):
        if v in ("", "null", None):
            return None
        return v

    @field_validator("counterparty", "delegate_to", mode="before")
    @classmethod
    def _one_line(cls, v):
        return one_line(v)


class ExtractionResult(BaseModel):
    items: list[ExtractedItem] = []


# Tool schema handed to Claude. We force a tool call so output is structured.
# The model only RECORDS data here — it has no action-taking tools (safety boundary).
TOOL_NAME = "record_action_items"

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Record commitments and expectations extracted from the email thread. "
        "Return an empty list if the thread contains no actionable items."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [t.value for t in MAIL_ACTION_TYPES],
                            "description": (
                                "MY_COMMITMENT: I promised to do something. "
                                "DELEGATED_TASK: I already asked someone else to do something. "
                                "TO_DELEGATE: an incoming request/task that I should hand off "
                                "to one of my teams rather than do myself. "
                                "WAITING_FOR_REPLY: my last message asks a question/request "
                                "and I await a substantive reply; a closing 'ok/approved/"
                                "thanks' that ends the exchange is NOT this (use INFO). "
                                "NEED_MY_REPLY: someone asked me something, I must reply. "
                                "INFO: informational only, no action."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": (
                                "Short imperative summary of the item, in Russian, "
                                "at most 60 characters."
                            ),
                        },
                        "counterparty": {
                            "type": ["string", "null"],
                            "description": (
                                "The other party, ONE line: a person's name or email, no "
                                "commentary. NEED_MY_REPLY: who waits for my reply (the "
                                "last inbound sender); WAITING_FOR_REPLY / DELEGATED_TASK: "
                                "whom I asked; MY_COMMITMENT: whom I promised. null when "
                                "there is no one."
                            ),
                        },
                        "delegate_to": {
                            "type": ["string", "null"],
                            "description": (
                                "ONLY for TO_DELEGATE: the team or person to hand this off to, "
                                "chosen from the owner profile's teams. null if no team clearly "
                                "fits. Leave null for every other type."
                            ),
                        },
                        "due_date": {
                            "type": ["string", "null"],
                            "description": (
                                "Deadline as YYYY-MM-DD, or null. "
                                "Resolve relative dates against TODAY."
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "description": "0..1 self-estimate. Soft signal only.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Brief justification citing the thread, in Russian.",
                        },
                    },
                    "required": ["type", "title", "confidence"],
                },
            }
        },
        "required": ["items"],
    },
}


# --- Project classification ---


class ProjectMatch(BaseModel):
    project: str  # the project name, echoed back so we can map to its id
    member: bool
    confidence: float = 0.5
    reason: str | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def _empty_reason(cls, v):
        if v in ("", "null"):
            return None
        return v


class ProjectMatchResult(BaseModel):
    matches: list[ProjectMatch] = []


PROJECT_TOOL_NAME = "record_project_matches"

PROJECT_TOOL_SCHEMA = {
    "name": PROJECT_TOOL_NAME,
    "description": (
        "Decide which of the listed projects this email thread belongs to. A thread may "
        "belong to several projects or none. Honor exclusions stated in each project's "
        "description. Return one entry per project you considered."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": "The exact project name from the list.",
                        },
                        "member": {
                            "type": "boolean",
                            "description": "True if the thread belongs to this project.",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "0..1 self-estimate.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Brief justification, in Russian.",
                        },
                    },
                    "required": ["project", "member", "confidence"],
                },
            }
        },
        "required": ["matches"],
    },
}


# --- Meeting routing (summary + items + project verdicts in one call) ---

# Types a MEETING can produce. WAITING_FOR_REPLY / NEED_MY_REPLY are about an exchange of
# messages and have no meaning for a conversation that already happened.
MEETING_ACTION_TYPES = [
    ActionType.MY_COMMITMENT,
    ActionType.DELEGATED_TASK,
    ActionType.TO_DELEGATE,
    ActionType.TASK,
    ActionType.DECISION,
    # INFO со встреч нет: наблюдений в разговоре десятки, их место — в summary (27.09.2026).
]


class MeetingItem(BaseModel):
    type: ActionType
    title: str
    counterparty: str | None = None
    due_date: date | None = None
    at: str | None = None            # timecode in the transcript, «00:23:10»
    project_hint: str | None = None  # a hint only — membership is decided for the meeting
    confidence: float = 0.5
    reason: str | None = None

    @field_validator("due_date", mode="before")
    @classmethod
    def _empty_date(cls, v):
        if v in ("", "null", None):
            return None
        return v

    @field_validator("at", "project_hint", "reason", mode="before")
    @classmethod
    def _empty_str(cls, v):
        if v in ("", "null"):
            return None
        return v

    @field_validator("counterparty", mode="before")
    @classmethod
    def _one_line(cls, v):
        return one_line(v)


class MeetingMatch(BaseModel):
    project: str  # the project name, echoed back so we can map it to an id
    member: bool
    confidence: float = 0.5
    reason: str | None = None
    digest: str | None = None  # what this meeting means FOR THIS project

    @field_validator("reason", "digest", mode="before")
    @classmethod
    def _empty_str(cls, v):
        if v in ("", "null"):
            return None
        return v


class MeetingRoutingResult(BaseModel):
    title: str | None = None
    summary: str | None = None
    # Сводка по разделам — полями, а не форматом текста: текстовому формату модель
    # следовала в 4 случаях из 5 (27.09.2026). summary собирается из них.
    about: str | None = None
    discussed: list[str] = []
    agreed: list[str] = []
    open_questions: list[str] = []
    participants: list[str] = []
    items: list[MeetingItem] = []
    matches: list[MeetingMatch] = []

    @model_validator(mode="after")
    def _summary_from_sections(self):
        if (self.summary or "").strip() or not (
            self.about or self.discussed or self.agreed or self.open_questions
        ):
            return self
        lines = [f"О чём: {self.about.strip()}"] if (self.about or "").strip() else []
        for head, points in (("Обсудили", self.discussed), ("Договорились", self.agreed),
                             ("Открытые вопросы", self.open_questions)):
            points = [p.strip() for p in points if p and p.strip()]
            if points:
                lines.append(f"{head}:")
                lines += [f"- {p}" for p in points]
        self.summary = "\n".join(lines) or None
        return self

    @field_validator("items", mode="before")
    @classmethod
    def _drop_unusable_items(cls, v):
        """A stray type in one item must not cost us the whole extraction — cheap models
        do emit NEED_MY_REPLY or invented labels here."""
        if not isinstance(v, list):
            return []
        allowed = {t.value for t in MEETING_ACTION_TYPES}
        out = []
        for i in v:
            # Either raw tool output (dicts) or already-built items (callers, tests).
            type_ = i.get("type") if isinstance(i, dict) else getattr(i, "type", None)
            title = i.get("title") if isinstance(i, dict) else getattr(i, "title", None)
            if type_ in allowed and str(title or "").strip():
                out.append(i)
        return out


MEETING_TOOL_NAME = "record_meeting_routing"

MEETING_TOOL_SCHEMA = {
    "name": MEETING_TOOL_NAME,
    "description": (
        "Record what a recorded meeting produced: a short summary, the tasks/decisions/"
        "facts stated in it, and — for each listed project — whether the meeting belongs "
        "to it. A meeting may belong to several projects or none."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "What the meeting was ABOUT, in Russian, 3-7 words. If the "
                    "participants say what the meeting is («планёрка по поставкам», "
                    "«созвон по проекту Альфа»), take their wording; otherwise name the topic "
                    "yourself from the content. Never the opening phrase or a greeting."
                ),
            },
            "about": {
                "type": "string",
                "description": ("О чём встреча целиком: одно-два предложения — зачем "
                                "собрались и кто участвовал. Обо мне — в первом лице."),
            },
            "discussed": {
                "type": "array",
                "items": {"type": "string"},
                "description": ("Что обсудили: по строке на тему — суть, наблюдения, "
                                "мнения, проблемы, цифры."),
            },
            "agreed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "О чём договорились: «кто — что — к какому сроку». Нет — пусто.",
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Что осталось нерешённым. Нет — пусто.",
            },
            "participants": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Speaker names if the transcript names them; else empty.",
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [t.value for t in MEETING_ACTION_TYPES],
                            "description": (
                                "MY_COMMITMENT: I took something on. "
                                "DELEGATED_TASK: I asked someone else to do it. "
                                "TO_DELEGATE: work that should be handed to one of my teams. "
                                "TASK: something I will do myself that no one in particular "
                                "is owed (a task to self). "
                                "DECISION: a final decision that changes further work "
                                "(no owner, no deadline); usually 0-3 per meeting. "
                                "Facts and observations are NOT items — they go to "
                                "`discussed`."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": (
                                "Short summary of the item, in Russian, at most 60 "
                                "characters."
                            ),
                        },
                        "counterparty": {
                            "type": ["string", "null"],
                            "description": "Who owes it / who it concerns. null if nobody.",
                        },
                        "due_date": {
                            "type": ["string", "null"],
                            "description": (
                                "Deadline as YYYY-MM-DD, or null. "
                                "Resolve relative dates («к пятнице») against TODAY."
                            ),
                        },
                        "at": {
                            "type": ["string", "null"],
                            "description": "Timecode where it was said, HH:MM:SS, if known.",
                        },
                        "project_hint": {
                            "type": ["string", "null"],
                            "description": "Project name it seems to concern, or null.",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "0..1 self-estimate.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Brief justification citing the meeting, in Russian.",
                        },
                    },
                    "required": ["type", "title", "confidence"],
                },
            },
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": "The exact project name from the list.",
                        },
                        "member": {
                            "type": "boolean",
                            "description": "True if the meeting belongs to this project.",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "0..1 self-estimate.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Brief justification, in Russian.",
                        },
                        "digest": {
                            "type": "string",
                            "description": (
                                "Подробный разбор ФРАГМЕНТА разговора, относящегося "
                                "именно к этому проекту, по-русски. Ровно четыре абзаца "
                                "в этом порядке: «**О чём фрагмент.**», «**Что "
                                "обсуждали.**» (маркеры с тайм-кодами), «**К чему "
                                "пришли.**», «**Открытые вопросы.**». Остальную встречу "
                                "не пересказывать. Только при member=true."
                            ),
                        },
                    },
                    "required": ["project", "member", "confidence"],
                },
            },
        },
        "required": ["items", "matches"],
    },
}


# --- Advice on a card (docs/feature-card-advice.md §1.3) ---

# What the owner should do next. Kinds `type` cannot express are the point: wait, close as
# a duplicate of another card, escalate, bundle with another card.
ADVICE_KINDS = [
    "reply", "delegate", "followup", "wait", "close_duplicate", "escalate", "bundle",
]


def _int_list(v):
    """Evidence numbers as ints; a stray string or None becomes nothing, not an error."""
    if v is None:
        return []
    if not isinstance(v, list):
        v = [v]
    out = []
    for x in v:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


class Claim(BaseModel):
    text: str = ""
    refs: list[int] = []

    @field_validator("refs", mode="before")
    @classmethod
    def _refs(cls, v):
        return _int_list(v)


class Recommendation(BaseModel):
    kind: str = ""
    why: str = ""
    refs: list[int] = []
    target: int | None = None

    @field_validator("refs", mode="before")
    @classmethod
    def _refs(cls, v):
        return _int_list(v)

    @field_validator("target", mode="before")
    @classmethod
    def _target(cls, v):
        got = _int_list(v)
        return got[0] if got else None


class AdviceResult(BaseModel):
    importance: Claim | None = None
    recommendation: Recommendation | None = None
    points: list[Claim] = []
    risk: Claim | None = None

    @field_validator("recommendation", "importance", "risk", mode="before")
    @classmethod
    def _empty_object(cls, v):
        return None if v in ("", "null", {}) else v


# --- The daily briefing (docs/feature-memory.md §3, stage 5) ---


class BriefingEntry(BaseModel):
    n: int = 0
    why: str = ""
    to: str | None = None
    refs: list[int] = []

    @field_validator("n", mode="before")
    @classmethod
    def _n(cls, v):
        got = _int_list(v)
        return got[0] if got else 0

    @field_validator("refs", mode="before")
    @classmethod
    def _refs(cls, v):
        return _int_list(v)


class BriefingResult(BaseModel):
    focus: list[BriefingEntry] = []
    delegate: list[BriefingEntry] = []
    later: list[BriefingEntry] = []
    note: str = ""


BRIEFING_TOOL_NAME = "record_briefing"

_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "n": {"type": "integer", "description": "Номер карточки из списка."},
        "why": {"type": "string", "description": "Одно короткое предложение по-русски."},
        "refs": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "ОБЯЗАТЕЛЬНО: номера ДРУГИХ карточек списка, из-за которых эта попала "
                "сюда — тот же человек, тот же проект, та же работа. Нет таких — []."
            ),
        },
    },
    "required": ["n", "why", "refs"],
}

BRIEFING_TOOL_SCHEMA = {
    "name": BRIEFING_TOOL_NAME,
    "description": (
        "Record the day's briefing: what to do first, what to hand off, what can wait — "
        "each pick justified and linked to the other cards it stands with."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "focus": {
                "type": "array",
                "items": _ENTRY_SCHEMA,
                "description": "До трёх карточек, за которые Я берусь сам сегодня.",
            },
            "delegate": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        **_ENTRY_SCHEMA["properties"],
                        "to": {
                            "type": "string",
                            "description": "Точное имя команды или человека из карточки/профиля.",
                        },
                    },
                    "required": ["n", "why", "to", "refs"],
                },
                "description": "До трёх карточек, которые стоит отдать.",
            },
            "later": {
                "type": "array",
                "items": _ENTRY_SCHEMA,
                "description": "До десяти карточек, которые могут подождать.",
            },
            "note": {
                "type": "string",
                "description": "Одно предложение о дне в целом, без вступлений.",
            },
        },
        "required": ["focus", "delegate", "later", "note"],
    },
}


ADVICE_TOOL_NAME = "record_advice"

_CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Одно предложение по-русски."},
        "refs": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Номера улик, на которых стоит это предложение.",
        },
    },
    "required": ["text", "refs"],
}

ADVICE_TOOL_SCHEMA = {
    "name": ADVICE_TOOL_NAME,
    "description": (
        "Record what the owner should do with this card: the one recommended move and "
        "what the thread is about, why it matters now, up to three things to do, the cost "
        "of doing nothing — every claim grounded in the numbered evidence."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "importance": {
                "type": ["object", "null"],
                "properties": _CLAIM_SCHEMA["properties"],
                "description": (
                    "Почему это важно именно сейчас — ОДНО предложение до 120 знаков: "
                    "срок, кто ждёт, что сорвётся. Степень важности не называй, её "
                    "считает приложение. Без улик — null."
                ),
            },
            "recommendation": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ADVICE_KINDS,
                        "description": (
                            "reply — ответить; delegate — передать команде или человеку "
                            "(кому — в why); followup — напомнить молчащему; wait — ждать, "
                            "действие не нужно; close_duplicate — это дубль карточки "
                            "target; escalate — поднять вопрос выше; bundle — ответить "
                            "вместе с карточкой target."
                        ),
                    },
                    "why": {
                        "type": "string",
                        "description": (
                            "О чём тред и чего от меня хотят — одно предложение до 140 "
                            "знаков: кто, что просит, к какому сроку. Не пересказ письма."
                        ),
                    },
                    "refs": {"type": "array", "items": {"type": "integer"}},
                    "target": {
                        "type": ["integer", "null"],
                        "description": "Для close_duplicate и bundle — номер улики-карточки.",
                    },
                },
                "required": ["kind", "why", "refs"],
            },
            "points": {
                "type": "array",
                "items": _CLAIM_SCHEMA,
                "description": (
                    "До трёх пунктов, что сделать — каждый с глагола и до 90 знаков: "
                    "ответить на…, подтвердить…, уточнить…, спросить…, предложить…. "
                    "Наблюдения о положении дел сюда не идут: пункт — это действие."
                ),
            },
            "risk": {
                "type": ["object", "null"],
                "properties": _CLAIM_SCHEMA["properties"],
                "description": (
                    "Что будет, если ничего не делать — одно предложение до 140 знаков и "
                    "только когда улики это показывают (срок, просрочено, люди ждут). "
                    "Иначе null."
                ),
            },
        },
        "required": ["recommendation", "points"],
    },
}
