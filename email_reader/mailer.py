"""Outgoing mail (manual send). SMTP over SSL, run off the event loop.

Human-initiated only — there is no agent/LLM path to send. Builds a proper reply
(In-Reply-To / References / Re:), and APPENDs a copy to the Sent folder for providers
that don't auto-save SMTP sends (Gmail does, so we skip APPEND there to avoid duplicates).
"""

from __future__ import annotations

import asyncio
import imaplib
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from email_reader.models import Account
from email_reader.providers import PROVIDERS


def resolve_smtp(account: Account) -> tuple[str, int]:
    if account.smtp_host:
        return account.smtp_host, account.smtp_port or 465
    preset = PROVIDERS.get(account.provider)
    if preset and preset.get("smtp_host"):
        return preset["smtp_host"], preset.get("smtp_port", 465)
    # Last resort: derive from the IMAP host.
    return account.host.replace("imap.", "smtp.", 1), account.smtp_port or 465


def build_message(
    from_addr: str,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    attachments: list[dict] | None = None,
    html_body: str | None = None,
    cc: str = "",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    if cc:
        msg["Cc"] = cc  # smtplib.send_message() delivers to To+Cc automatically
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg["Date"] = formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    refs = list(references or [])
    if in_reply_to and in_reply_to not in refs:
        refs.append(in_reply_to)
    if refs:
        msg["References"] = " ".join(refs)
    # Plain-text part is always set; when html_body is given, add it as an alternative
    # (multipart/alternative) so clients pick HTML but old ones fall back to text.
    msg.set_content(body or "")
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for a in attachments or []:
        maintype, _, subtype = (a.get("mime") or "application/octet-stream").partition("/")
        msg.add_attachment(
            a["bytes"], maintype=maintype or "application",
            subtype=subtype or "octet-stream", filename=a.get("name") or "attachment",
        )
    return msg


def _send_blocking(
    smtp_host: str,
    smtp_port: int,
    login: str,
    password: str,
    msg: EmailMessage,
    imap_host: str | None,
    sent_folder: str | None,
) -> None:
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=30) as s:
        s.login(login, password)
        s.send_message(msg)

    # Save a copy to Sent for providers that don't auto-save (Gmail does -> skip).
    if imap_host and sent_folder and "gmail" not in imap_host.lower():
        try:
            with imaplib.IMAP4_SSL(imap_host, 993) as conn:
                conn.login(login, password)
                conn.append(
                    sent_folder, "\\Seen", imaplib.Time2Internaldate(time.time()),
                    msg.as_bytes(),
                )
        except Exception:  # noqa: BLE001 — failing to archive must not fail the send
            pass


async def send_mail(
    account: Account,
    password: str,
    *,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    attachments: list[dict] | None = None,
    html_body: str | None = None,
    cc: str = "",
) -> EmailMessage:
    """Send the message; return the built EmailMessage so the caller can store a copy."""
    smtp_host, smtp_port = resolve_smtp(account)
    msg = build_message(
        account.login, to, subject, body, in_reply_to, references, attachments,
        html_body=html_body, cc=cc,
    )
    sent_folder = account.folders[1] if len(account.folders) > 1 else "Sent"
    await asyncio.to_thread(
        _send_blocking,
        smtp_host, smtp_port, account.login, password, msg, account.host, sent_folder,
    )
    return msg
