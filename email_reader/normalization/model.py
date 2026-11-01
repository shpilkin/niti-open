"""Unified Email Model — provider-agnostic representation of one message."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class EmailAddress(BaseModel):
    name: str | None = None
    email: str


class ParsedEmail(BaseModel):
    """Normalized email, before storage. Direction/dedup are filled by callers."""

    rfc_message_id: str | None = None
    from_: EmailAddress = Field(alias="from")
    to: list[EmailAddress] = Field(default_factory=list)
    cc: list[EmailAddress] = Field(default_factory=list)
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    sent_at: datetime
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    has_attachments: bool = False

    model_config = {"populate_by_name": True}

    @property
    def all_recipient_emails(self) -> list[str]:
        return [a.email for a in (self.to + self.cc)]
