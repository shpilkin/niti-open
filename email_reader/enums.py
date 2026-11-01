"""Shared enums/string constants used across layers."""

from __future__ import annotations

from enum import StrEnum


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    SELF = "self"


class ActionType(StrEnum):
    MY_COMMITMENT = "MY_COMMITMENT"
    DELEGATED_TASK = "DELEGATED_TASK"
    TO_DELEGATE = "TO_DELEGATE"  # incoming request that should be handed to my team
    WAITING_FOR_REPLY = "WAITING_FOR_REPLY"
    NEED_MY_REPLY = "NEED_MY_REPLY"
    # A task to myself: my court, nobody in particular is owed it. Quick-add and the
    # Telegram bot produce these; a meeting «promise» with no one to keep it to becomes
    # one too. The mail extractor never emits it (docs/ux-tasks.md §5, §7.1).
    TASK = "TASK"
    INFO = "INFO"
    # Preparation surfaced from the calendar (not extracted from email threads).
    # Created by the calendar gate, never emitted by the email extractor.
    MEETING_PREP = "MEETING_PREP"
    # Something settled at a meeting. Has no lifecycle — it can't be "closed" — so it
    # stays out of ACTIONABLE_TYPES, like INFO. Meetings only; the mail extractor is
    # never offered this type (see ai/schemas.MAIL_ACTION_TYPES).
    DECISION = "DECISION"


# Types that represent something requiring tracking (INFO is non-actionable).
# MEETING_PREP is intentionally excluded: it is not an email-extraction output.
ACTIONABLE_TYPES = {
    ActionType.MY_COMMITMENT,
    ActionType.DELEGATED_TASK,
    ActionType.TO_DELEGATE,
    ActionType.WAITING_FOR_REPLY,
    ActionType.NEED_MY_REPLY,
    ActionType.TASK,
}


class SourceKind(StrEnum):
    """Where an ActionItem came from. The FK columns (thread / event / meeting) still
    carry the reference; this is the discriminator every consumer switches on, so a new
    source is one enum value, not a fifth nullable FK and a fifth branch everywhere."""

    MAIL = "mail"            # extracted from (or created from) a mail thread
    MEETING = "meeting"      # extracted from a recorded meeting
    CALENDAR = "calendar"    # the retired prep board; archive only
    TELEGRAM = "telegram"    # sent to the mailbox's Telegram bot
    MANUAL = "manual"        # typed into quick-add


class IdentityKind(StrEnum):
    """How a person is identified in one row of `person_identities`.

    Only these two arrive as data: mail carries addresses, everything else (a transcript,
    a Telegram note, a typed task) carries a name the model read. A Telegram bot serves one
    user — its messages are the user's own, so there is no Telegram handle of somebody
    else to key a person on (docs/feature-people.md §2)."""

    EMAIL = "email"          # a normalized e-mail address
    NAME = "name"            # a person's name, folded to a comparison key


class CloseHint(StrEnum):
    """Почему ассистент предлагает закрыть карточку (`ActionItem.close_hint`).

    Предложение, не действие: закрывает пользователь. Причина нужна потому, что эти два
    случая требуют от него разного — в первом достаточно согласиться, во втором стоит
    напомнить о себе (docs/ux-tasks.md §17.4)."""

    # Собеседник написал после моего обещания или после поручения: похоже, вопрос закрыт.
    ACKNOWLEDGED = "acknowledged"
    # Месяц без движения: ни новых писем, ни правок.
    IDLE = "idle"


# Типы, которые затухают, если по ним долго ничего не происходит (docs/feature-topics.md
# §8.2). «Нужен ответ» — мой мяч, его не старят.
AGING_TYPES = (
    ActionType.DELEGATED_TASK,
    ActionType.TO_DELEGATE,
    ActionType.MY_COMMITMENT,
    ActionType.TASK,
    # Чужой ход тоже затухает (docs/feature-topics.md §8.2): по дороге он проходит
    # «пора напомнить». «Нужен ответ» не стареет никогда — он мой, пока я не ответил.
    ActionType.WAITING_FOR_REPLY,
)


# Human labels for ActionType (badges, tooltips, the «Исправить» select, bot replies).
TYPE_LABEL = {
    "NEED_MY_REPLY": "Нужен ответ",
    "WAITING_FOR_REPLY": "Жду ответа",
    "MY_COMMITMENT": "Моё обещание",
    "TASK": "Задача",
    "DELEGATED_TASK": "Делегировано",
    "TO_DELEGATE": "Передать команде",
    "INFO": "Для сведения",
    "DECISION": "Решение",
    "MEETING_PREP": "К встрече",
}


class ActionStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    SNOOZED = "snoozed"
    OVERDUE = "overdue"
    DONE = "done"
    DISMISSED = "dismissed"


# Statuses still "live" (shown in active views, eligible for auto-transitions).
LIVE_STATUSES = {ActionStatus.OPEN, ActionStatus.IN_PROGRESS, ActionStatus.OVERDUE}


class UserVerdict(StrEnum):
    CONFIRMED = "confirmed"
    EDITED = "edited"
    REJECTED = "rejected"


class EventType(StrEnum):
    EMAIL_STORED = "EmailStored"
    THREAD_READY = "ThreadReady"
    ACTION_ITEMS_EXTRACTED = "ActionItemsExtracted"
    FOLLOWUP_DUE = "FollowUpDue"
    PROJECT_MATCH = "ProjectMatch"
    MARK_SEEN = "MarkSeen"  # write local read-state back to the IMAP server (\Seen)
    # A transcript arrived from the recorder app and was indexed as a Meeting.
    MEETING_READY = "MeetingReady"
    # Read the next batch of a space's own letters / meetings for facts to propose
    # (memory.capture); each event does one batch and queues the next.
    FACTS_CAPTURE = "FactsCapture"
    # Посчитать совет «Что делать» для карточки — в фоне, чтобы его не ждала панель.
    ADVICE_NEEDED = "AdviceNeeded"


class EventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    # Transient: the model/infra was unavailable (e.g. 403/429/5xx, connection reset).
    # Parked without burning the retry budget; the worker revives these once a model
    # health-check passes again. Distinct from FAILED (a genuine, permanent error).
    STALLED = "stalled"


class PolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"
