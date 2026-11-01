"""SQLAlchemy ORM models. Mirrors docs/03-technical-design.md §3."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from email_reader.dbtypes import UTCDateTime

# Dialect-portable defaults. UUID/JSON values are produced by the Python-side
# ``default=`` callables below, so no SQL server_default is needed for them
# (Postgres' gen_random_uuid()/::jsonb casts don't exist on SQLite).
_FALSE = false()
_TRUE = true()

# Generic UUID column: native uuid on Postgres, CHAR(32) on SQLite.
UUID = Uuid
# Generic JSON column: JSONB on Postgres, TEXT(json) on SQLite.
JSONB = JSON


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


class Space(Base):
    """A context boundary — «Работа», «Личное» (docs/spaces.md). The sources inside it
    (mailboxes, Telegram bots, meetings) commingle on one set of boards, one dossier per
    person, one model; nothing crosses to another space. Owns what used to hang on the
    mailbox: the model, the prompts, the profile. Signatures stay on the mailbox — they
    are the sender's, not the context's."""

    __tablename__ = "spaces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # The model that reads everything in this space. NULL -> the global default.
    model_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class BoardSpec(Base):
    """Одна доска пространства: как её звать, что на ней и где она в сайдбаре.

    Доска — запрос по индексированным колонкам, поэтому её параметры можно держать в базе и
    отдать в настройки (docs/ux-tasks.md §17.5). `kind` делит записи на две породы: у
    пресета предикат и секции живут в Python (`views.py`), у `filter` — в этой строке.

    `key` уходит в URL (`/view/<key>`) и в имя cookie группировки, поэтому он не
    транслитерация имени: переименование доски не должно ломать ссылку. У пресетов ключ
    равен виду — старые ссылки и знание бота о досках остаются в силе."""

    __tablename__ = "board_specs"
    __table_args__ = (UniqueConstraint("space_id", "key", name="uq_board_space_key"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    # boards.PRESET_KINDS или boards.FILTER.
    kind: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    icon: Mapped[str] = mapped_column(String, nullable=False, default="dot")
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=_FALSE
    )
    # Пресет нельзя удалить — его ключ знают бот, MCP и сохранённые ссылки; его прячут.
    builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=_FALSE
    )
    # Дальше — только для kind=filter.
    types: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    condition: Mapped[str | None] = mapped_column(String, nullable=True)
    sort: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class TelegramBot(Base):
    """A Telegram bot that feeds one space: what is written to it lands in that space's
    «Разобрать» (docs/ux-tasks.md §12, docs/spaces.md §3). Token encrypted like a mailbox
    password; chat_id is the one chat the bot serves (found via /start)."""

    __tablename__ = "telegram_bots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=_TRUE
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # The space this mailbox feeds. A mailbox is a source; the space is the boundary.
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(
        String, nullable=False, default="imap", server_default="imap"
    )
    host: Mapped[str] = mapped_column(String, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=993, server_default="993")
    smtp_host: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp_port: Mapped[int] = mapped_column(
        Integer, nullable=False, default=465, server_default="465"
    )
    login: Mapped[str] = mapped_column(String, nullable=False)
    # Env-var name holding the app-password (config-imported accounts). Nullable now that
    # UI-added accounts store the password encrypted in password_enc instead.
    password_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional separate app-password for CalDAV (Yandex scopes app-passwords per service,
    # so the IMAP one is rejected by the calendar). NULL -> fall back to password_enc.
    caldav_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    folders: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=_TRUE
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class SyncState(Base):
    """Per-(account, folder) IMAP cursor."""

    __tablename__ = "sync_state"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), primary_key=True
    )
    folder: Mapped[str] = mapped_column(String, primary_key=True)
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_uid: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), onupdate=func.now()
    )


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    # Owning mailbox: a thread belongs to exactly one account. Cross-account
    # conversations stay as separate threads (strict mailbox isolation).
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=True, index=True
    )
    subject_norm: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    first_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Bumped whenever the thread changes -> triggers re-extraction.
    extract_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Version actually extracted (for idempotency).
    extracted_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=-1, server_default="-1"
    )

    emails: Mapped[list[Email]] = relationship(back_populates="thread")


class Email(Base):
    __tablename__ = "emails"
    # Dedup is per-account (strict mailbox isolation): the same message in two of the
    # user's mailboxes is two separate emails, so the Message-ID is unique per account,
    # not globally.
    __table_args__ = (
        UniqueConstraint("account_id", "rfc_message_id", name="uq_email_account_msgid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    # Owning mailbox. Drives which model sees the email and keeps mailboxes separated.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=True, index=True
    )
    # Per-account dedup key (RFC Message-ID). May be NULL -> fallback to dedup_hash.
    rfc_message_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    dedup_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("threads.id"), nullable=True, index=True
    )
    direction: Mapped[str] = mapped_column(String, nullable=False)
    from_name: Mapped[str | None] = mapped_column(String, nullable=True)
    from_addr: Mapped[str] = mapped_column(String, nullable=False, index=True)
    to_addrs: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    cc_addrs: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_raw_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String, nullable=True)
    reference_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    headers: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    is_low_value: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=_FALSE
    )
    has_attachments: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=_FALSE
    )
    raw_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # Local read state (NULL = unread). Never synced to the server — IMAP stays read-only.
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())

    thread: Mapped[Thread | None] = relationship(back_populates="emails")
    occurrences: Mapped[list[EmailOccurrence]] = relationship(back_populates="email")


class EmailOccurrence(Base):
    """One email may appear in several mailboxes/folders (self-CC, cross-account copies)."""

    __tablename__ = "email_occurrences"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), primary_key=True
    )
    folder: Mapped[str] = mapped_column(String, primary_key=True)
    uid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("emails.id"), nullable=False, index=True
    )

    email: Mapped[Email] = relationship(back_populates="occurrences")


class Draft(Base):
    """Недописанное письмо: то, что человек набрал и бросил, не отправив.

    Вложения не хранятся — файлы приходят в форме только при отправке, автосохранение
    их не видит.
    """


    __tablename__ = "drafts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False, index=True
    )
    to_line: Mapped[str] = mapped_column(String, nullable=False, default="")
    cc_line: Mapped[str] = mapped_column(String, nullable=False, default="")
    subject: Mapped[str] = mapped_column(String, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Контекст ответа/пересылки: без него продолженный черновик уходит вне переписки.
    in_reply_to: Mapped[str] = mapped_column(String, nullable=False, default="")
    references: Mapped[str] = mapped_column(Text, nullable=False, default="")
    forward_email_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("emails.id"), nullable=True
    )
    back_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), onupdate=func.now()
    )


class AppSetting(Base):
    """Generic key-value store for UI-editable app settings (e.g. AI model/api key).

    Secrets are stored Fernet-encrypted (is_secret=True); non-secrets in plain text.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_secret: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=_FALSE
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), onupdate=func.now()
    )


class ModelConnection(Base):
    """A configured LLM endpoint. kind='anthropic' (native) or 'openai' (OpenAI-compatible
    gateway: OpenRouter / RouterAI / LiteLLM / custom). One row is the default used
    for extraction. API key is Fernet-encrypted; for native Anthropic it may be null
    (falls back to ANTHROPIC_API_KEY)."""

    __tablename__ = "model_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)  # preset key
    kind: Mapped[str] = mapped_column(String, nullable=False)  # anthropic | openai
    base_url: Mapped[str | None] = mapped_column(String, nullable=True)
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String, nullable=False)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=_FALSE
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=_TRUE
    )
    # Ask the gateway to answer without reasoning (chat_template_kwargs.enable_thinking).
    no_thinking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=_FALSE
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class Identity(Base):
    """An email address that belongs to the user ("me"). Editable from the UI."""

    __tablename__ = "identities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class Label(Base):
    __tablename__ = "labels"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    color: Mapped[str] = mapped_column(
        String, nullable=False, default="#9aa0a6", server_default="#9aa0a6"
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class SenderRule(Base):
    """If a correspondent matches (address or domain), do `action`: attach a label
    (label_id), treat the sender's mail as low value (`ignore`: no extraction), or hand the
    sender's requests to `value` (`delegate`: cards become TO_DELEGATE). Global — a rule
    about a sender does not depend on the space."""

    __tablename__ = "sender_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    match_type: Mapped[str] = mapped_column(String, nullable=False)  # address | domain
    pattern: Mapped[str] = mapped_column(String, nullable=False, index=True)  # normalized lower
    action: Mapped[str] = mapped_column(
        String, nullable=False, default="label", server_default="label"
    )
    value: Mapped[str | None] = mapped_column(String, nullable=True)  # delegate: who
    label_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("labels.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class EmailLabel(Base):
    """Метка стоит на письме, а не на беседе: правило заводят на адрес, и метку должны
    получать письма именно этого человека, а не вся переписка, в которой он написал."""

    __tablename__ = "email_labels"

    email_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("emails.id", ondelete="CASCADE"), primary_key=True
    )
    label_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("labels.id", ondelete="CASCADE"), primary_key=True
    )


class Person(Base):
    """One human being — the profile the user's memory hangs on.

    A person reaches Niti under several names: an address in the mail, a name the model
    read off a transcript, a name typed into a quick task. Each of those is a
    `PersonIdentity` row; the profile is what they were merged into (docs/feature-people.md).
    Global across spaces by decision (docs/feature-memory.md §2.1) — who someone is does
    not depend on which mailbox they wrote to."""

    __tablename__ = "people"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    # The primary address, kept because PersonStat and SenderRule are address-keyed.
    # NULL for a profile known only by name (from a meeting, Telegram or a typed task).
    email: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    is_me: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Stamped whenever the memory on this person changes (memory.facts) — the pane shows it.
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class MemoryFact(Base):
    """One thing the owner knows, as a row: what the space profile and the notes on a
    person are made of (docs/feature-memory-facts.md §3).

    `subject_kind`/`subject_key` say what the fact is about: `me` (per space — role, teams,
    how things are done here; `space_id` set) or `person` (global; `subject_key` is the
    person id). `evidence_kind` says where it came from: the profile editor, the person
    pane, or «Запомнить». A fact is never deleted — retiring sets `valid_to`, and the row
    that replaced it points back through `supersedes_id`."""

    __tablename__ = "memory_facts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # NULL = global (facts about a person); a space's own memory goes with the space.
    space_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=True
    )
    subject_kind: Mapped[str] = mapped_column(String, nullable=False)  # me | person
    subject_key: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="fact")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # profile | notes | remember | mail | turn (memory.facts)
    evidence_kind: Mapped[str] = mapped_column(String, nullable=False)
    evidence_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # proposed (waits for the owner) | active | rejected. Only active reaches a prompt.
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Order within a subject: the line order of the text the facts render back into.
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    # A captured fact (memory.capture): how sure the model was, the words of the source
    # it quoted, and the space whose letter or meeting the proposal came from — a fact
    # about a person is global once confirmed, its evidence is not.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_in: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="SET NULL"), nullable=True
    )
    # A person the address book does not know yet: the name as the source wrote it,
    # until the owner says yes and the profile is made.
    subject_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # When the owner said yes, and when a source last said it again — what «устарело?»
    # is judged by.
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        Index("ix_memory_facts_subject", "space_id", "subject_kind", "subject_key", "status"),
        Index("ix_memory_facts_evidence", "evidence_kind", "evidence_id"),
    )


class PersonIdentity(Base):
    """One way a person shows up in the data: an e-mail address, or a name.

    `value` is the normalized key (lowercased address, or a name folded to lowercase with
    collapsed spaces) and is unique per kind across all profiles — one address belongs to
    exactly one person. `display` keeps the form the user should read. Merging a profile
    into another one only re-points these rows, so nothing else has to know about it."""

    __tablename__ = "person_identities"
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_person_identity"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    # IdentityKind: "email" | "name".
    kind: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(String, nullable=False)
    display: Mapped[str | None] = mapped_column(String, nullable=True)
    # Where it was first seen: "mail" | "meeting" | "telegram" | "manual" — shown as a chip
    # so the user can tell why a name is in their list.
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class PersonStat(Base):
    """memory.stats.person_stats cached per (space, address), so boards can order rows by
    the sender history without recomputing it per row. Refreshed at extraction time and by
    the worker's idle sweep (memory.stats.refresh_stale)."""

    __tablename__ = "person_stats"

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True
    )
    email: Mapped[str] = mapped_column(String, primary_key=True)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    inbound: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inbound_to_me: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outbound: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replied: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unanswered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_inbound: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    median_reply_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


def _infer_source_kind(context) -> str:
    """Default for ActionItem.source_kind: read the FK the caller did set. Keeps every
    ActionItem(...) that predates the column honest — a meeting row says «meeting»."""
    from email_reader.enums import SourceKind

    p = context.get_current_parameters()
    if p.get("source_meeting_id") is not None:
        return SourceKind.MEETING.value
    if p.get("source_event_id") is not None:
        return SourceKind.CALENDAR.value
    if p.get("source_thread_id") is not None:
        return SourceKind.MAIL.value
    return SourceKind.MANUAL.value


class ActionItem(Base):
    __tablename__ = "action_items"
    __table_args__ = (
        # Dedup key for upsert across re-extractions of the same thread.
        UniqueConstraint(
            "source_thread_id", "type", "counterparty", name="uq_action_item_source"
        ),
        # Event-sourced items mirror the thread key: one item per (event, type,
        # counterparty). Nothing writes these any more — the calendar prep board and its
        # gate were removed — but the archive still holds them, so the key stays.
        UniqueConstraint(
            "source_event_id", "type", "counterparty", name="uq_action_item_event"
        ),
        # Meetings key on the title too: unlike a thread (one evolving obligation per
        # counterparty) a single recording legitimately yields several distinct tasks for
        # the same person. Fourth source reference — the honest limit of this pattern:
        # a fifth would have to become (source_kind, source_id).
        UniqueConstraint(
            "source_meeting_id", "type", "counterparty", "title",
            name="uq_action_item_meeting",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    counterparty: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # For TO_DELEGATE items: the team/person the assistant suggests handing this off to,
    # picked from the mailbox profile. NULL for other types (or when unclear).
    delegate_to: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="open", server_default="open", index=True
    )
    next_check: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    source_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("threads.id"), nullable=True
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("emails.id"), nullable=True
    )
    # Calendar-sourced items (MEETING_PREP) point here instead of a thread/email. Mirrors
    # the source_thread_id/source_message_id pattern; keeps mailboxes isolated via event.account_id.
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calendar_events.id"), nullable=True
    )
    # Items extracted from a recorded meeting (tasks, decisions, facts).
    source_meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id"), nullable=True
    )
    # Прямая привязка к Нити: для карточек без беседы и встречи (Telegram, вручную) и
    # когда Нить указана явно. Сильнее принадлежности через источник — правило одно на
    # всех читателей: `projects.card_member` (docs/feature-topics.md §8.3).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True,
        index=True,
    )
    # Discriminator over the source references above (enums.SourceKind). Filled at
    # creation; inferred from whichever FK is set when a caller does not say.
    source_kind: Mapped[str] = mapped_column(
        String, nullable=False, default=_infer_source_kind, server_default="mail", index=True
    )
    # The space this item lives in — the one thing every board, dossier and count filters
    # on. Denormalised on purpose: boards are the hot path, and items from Telegram,
    # meetings and quick-add have no thread to derive it from. NOT NULL: an item without
    # a space would be on no board at all, which is the silent failure we forbid.
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id"), nullable=False, index=True
    )
    # Opaque pointer into a source that has no table: «chat_id:message_id» for Telegram.
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True when the delegated-task counterparty replied -> candidate for closing (not auto-closed).
    # Почему ассистент предлагает закрыть: CloseHint. NULL, когда не предлагает.
    close_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    close_candidate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=_FALSE
    )

    # Feedback loop
    user_edited: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=_FALSE
    )
    user_verdict: Mapped[str | None] = mapped_column(String, nullable=True)
    # Who last moved the item into a resolved state ('user' | 'auto', NULL while live).
    # Provenance decouples "user said done" (final) from "automation retired it"
    # (may be revived when its reasons reappear) — user_edited alone conflated both.
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), onupdate=func.now()
    )


class AgentDecision(Base):
    """Trace of every automated decision (extract/auto_resolve/overdue/policy)."""

    __tablename__ = "agent_decisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    action_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("action_items.id"), nullable=True, index=True
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class Event(Base):
    """In-Postgres event queue (consumed via SELECT ... FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "events"

    # SQLite only auto-increments an INTEGER PRIMARY KEY (rowid alias); a BIGINT
    # PK would not. with_variant keeps BIGINT on Postgres, INTEGER on SQLite.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    available_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class EvalSample(Base):
    """Ground-truth labels harvested from user corrections + golden set."""

    __tablename__ = "eval_samples"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("threads.id"), nullable=True
    )
    expected: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())


class Project(Base):
    """A user-defined topic. A natural-language prompt describes what belongs to it;
    matching threads' emails + attachments are exported into folder_path on disk."""

    __tablename__ = "projects"
    # Имя уникально в пространстве, не глобально: «Договор» может быть и рабочей, и
    # личной Нитью (docs/feature-topics.md §8.6).
    __table_args__ = (UniqueConstraint("space_id", "name", name="uq_projects_space_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Owning space: a project groups the correspondence and meetings of one space, so its
    # classifier uses that space's model and only sees that space's threads.
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id"), nullable=False, index=True
    )
    # Explicit folder for this project's artifacts. Empty -> app_settings['projects_root']
    # plus the project name (see project_folder.folder_for).
    folder_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=_TRUE
    )
    # Opt-in for the meeting router: a deliberate per-project choice, so a project can
    # collect mail without having every meeting of the space classified against it.
    include_meetings: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=_FALSE
    )
    # Deterministic keywords: client/product/people names. An exact (case-insensitive)
    # occurrence in a transcript pins the meeting to this project regardless of the model.
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # «Отложить до…» у Нити целиком: одна дата и одно напоминание в брифинге вместо N
    # отложенных карточек (docs/feature-topics.md §8.4). Карточки свои даты не теряют.
    snoozed_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    # `active` — обычная Нить; `proposed` — предложил агент через MCP, ждёт «Да» владельца
    # и до него не участвует ни в маршрутизации, ни в списках (docs/feature-topics.md §9.1).
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    # Довод и улики предложения: {"why": …, "evidence": [...], "by": "<клиент MCP>"}.
    proposal: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Заметка агента о Нити — для человека на экране; в промпты не попадает.
    agent_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())

    threads: Mapped[list[ProjectThread]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    meetings: Mapped[list[ProjectMeeting]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectThread(Base):
    """Thread<->Project membership. source='auto' (agent) or 'manual' (user override);
    manual rows are never overwritten/removed by the classifier."""

    __tablename__ = "project_threads"
    __table_args__ = (
        UniqueConstraint("project_id", "thread_id", name="uq_project_thread"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("threads.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source: Mapped[str] = mapped_column(
        String, nullable=False, default="auto", server_default="auto"
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())

    project: Mapped[Project] = relationship(back_populates="threads")


class Meeting(Base):
    """A recorded conversation handed over by the transcription app.

    An index over files, not a copy of them: the transcript and audio stay in the ASR
    archive and Niti never writes there. Paths are relative to the archive root
    (``app_settings['meetings.asr_root']``) so moving the archive doesn't break the link.
    Dedup on ext_id — a hash of the audio content, so renaming a file cannot spawn a
    second meeting."""

    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # Stable id from the producer (sha256 prefix of the audio). Dedup key across re-deliveries.
    ext_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    # The space the meeting belongs to: the one configured for meetings at intake time
    # (meetings.routing_space_id); the user can move it afterwards.
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Speaker names once diarization can name them; ["Спикер 1", …] until then.
    participants: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Relative to the ASR root; NULL when the producer didn't ship that artifact.
    transcript_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    srt_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Producer identity ("recorder/1.0") — provenance when a whole batch looks wrong.
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    # Filled by the routing pass; NULL until then.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Projects the router is unsure about (the 0.5–0.8 zone): [{"project_id", "confidence",
    # "reason"}, …]. Not membership — nothing is written to disk for these until the user
    # confirms one. Rewritten wholesale by each routing pass.
    suggestions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Why a routing pass ended without doing anything (no mailbox configured for meetings,
    # no project opted in). NULL after a pass that ran — it is shown in the UI, because a
    # meeting that silently never routes is exactly the invisible failure we forbid.
    route_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    routed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Deleted by the user (soft): hidden from lists and counts, its machine-made items
    # resolved. Kept as a row because ext_id dedup must still recognise a re-delivered
    # recording as this meeting — and not resurrect it. Restorable from the undo toast.
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), onupdate=func.now()
    )


class ProjectMeeting(Base):
    """Meeting<->Project membership. Sister table to ProjectThread rather than a shared
    "artifact membership": zero migration risk for the mail paths, at the price of a few
    duplicated helpers. source='auto' (model), 'manual' (user), 'marker' (a project name
    or alias spoken/written in the transcript); manual rows survive re-classification."""

    __tablename__ = "project_meetings"
    __table_args__ = (
        UniqueConstraint("project_id", "meeting_id", name="uq_project_meeting"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source: Mapped[str] = mapped_column(
        String, nullable=False, default="auto", server_default="auto"
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What this meeting means FOR THIS project — the note in the project folder is written
    # from it, so three projects get three different texts over one recording.
    digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())

    project: Mapped[Project] = relationship(back_populates="meetings")


class CalendarEvent(Base):
    """A calendar event ingested from a mailbox's calendar (CalDAV in v1).

    Read-only mirror of the source event — like Email is for a mailbox message. Scoped by
    account_id (strict mailbox isolation). Dedup/upsert on (account_id, provider_uid).
    The prep memo generated by the agent is cached here (memo/memo_generated_at)."""

    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint("account_id", "provider_uid", name="uq_calevent_account_uid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # Owning mailbox: the calendar belongs to one account (isolation + board filter).
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=True, index=True
    )
    # Stable per-event id from the source (iCalendar UID) — dedup key across syncs.
    provider_uid: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Календарь внутри ящика («Мои события», «Работа») — по нему цвет в сетке.
    # NULL — событие не приходило с тех пор, как календарь начали запоминать.
    calendar_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # UID встречи из iCalendar: по нему Niti узнаёт ту же встречу под новым ключом.
    external_uid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    start_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    end_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    organizer: Mapped[str | None] = mapped_column(String, nullable=True)
    # List of attendees: [{"email": str, "name": str | None}, ...].
    attendees: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Set when the event disappears from the upstream calendar (cancelled/rescheduled);
    # cleared if it reappears. The prep gate skips cancelled events.
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Cached rule-1 LLM verdict ("does the description warrant prep?"). NULL = not asked
    # yet (or invalidated by a content change); avoids re-asking every sync.
    prep_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Cached prep memo (rendered in #pane). NULL until generated; reset on content change.
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    memo_generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), onupdate=func.now()
    )
