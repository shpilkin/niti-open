"""Compose/reply orchestration: prepare reply context and perform a human-initiated send.

Sending is logged to agent_decisions (kind='send') for the same audit trail as everything
else. There is no automated/agent send path — only this user-driven flow.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from html import escape as _html_escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import attachments as attachments_svc
from email_reader import htmlview, signatures
from email_reader.accounts import resolve_password
from email_reader.config import get_app_config
from email_reader.enums import Direction
from email_reader.identities import get_identity_set
from email_reader.ingest import store_email
from email_reader.mailer import send_mail
from email_reader.models import Account, AgentDecision, Email, EmailOccurrence
from email_reader.normalization import parse_raw_email

log = logging.getLogger("email_reader.compose")

# Marker that opens the forwarded-original block; also used to split the body so the
# signature can be placed BEFORE it (see _split_quote).
_FWD_MARKER = "---------- Пересланное письмо ----------"
# The reply-quote header ("<date>, <who> писал(а):") that opens a quoted reply. Allow a
# long sender display name (up to 200 chars) and a stray CR (defensive; body is normalized
# to \n before this runs) so the split doesn't silently miss and dump the signature below.
_REPLY_QUOTE_RE = re.compile(r"\n[^\n]{0,200}писал\(а\):\r?\n")
# Без white-space:pre-wrap: перевод строки несёт <br>, а pre-wrap сделал бы видимым
# ещё и то, как отформатирован исходник письма. Так же поступают почтовые клиенты.
_HTML_DIV = ("font-family:-apple-system,Segoe UI,Roboto,"
             "Helvetica,Arial,sans-serif;font-size:14px;color:#1a1a1a")
def _body_style(sig_html: str | None) -> str:
    """Стиль текста письма: шрифт берётся из подписи, если она его задаёт.

    Иначе набранный текст уходит системным шрифтом, а подпись — корпоративным, и
    письмо выглядит склеенным из двух разных."""
    face, size = htmlview.font_of(sig_html)
    if not face:
        return _HTML_DIV
    return f"font-family:{face};font-size:{size or '14px'};color:#1a1a1a"


def _readable_body(email: Email) -> str:
    """Текст письма для цитаты: та версия, что полнее.

    У части писем text/plain несёт только последнее сообщение, а всю
    переписку — text/html; цитата из короткой части обрывала цепочку."""
    text = (email.body_text or "").strip()
    if not email.body_raw_html:
        return text
    from_html = htmlview.html_to_text(email.body_raw_html)
    return from_html if len(from_html) > len(text) else text


def _quote_block(email: Email, kind: str) -> str:
    """Plain-text quote of the original message, pre-filled into the compose body so it
    is actually sent (forward) / quoted (reply) and remains editable."""
    who = email.from_name or email.from_addr
    to = ", ".join(a.get("email", "") for a in (email.to_addrs or []) if a.get("email"))
    when = email.sent_at.strftime("%d.%m.%Y %H:%M") if email.sent_at else "?"
    body = _readable_body(email)
    if kind == "forward":
        return (
            f"\n\n{_FWD_MARKER}\n"
            f"От: {who} <{email.from_addr}>\n"
            f"Дата: {when}\n"
            f"Кому: {to}\n"
            f"Тема: {email.subject or ''}\n\n"
            f"{body}\n"
        )
    quoted = "\n".join("> " + ln for ln in body.splitlines())
    return f"\n\n{when}, {who} писал(а):\n{quoted}\n"


def _reply_all_recipients(
    from_addr: str, to_addrs: list | None, cc_addrs: list | None, identities: set[str]
) -> tuple[str, str]:
    """Reply-all recipient lists: To = sender + original To minus me; CC = original CC
    minus me and minus anyone already in To. Order preserved, duplicates dropped."""
    from email_reader.normalization import normalize_addr

    seen: set[str] = set()
    to_list: list[str] = []
    for raw in [from_addr, *[(a or {}).get("email", "") for a in (to_addrs or [])]]:
        addr = normalize_addr(raw or "")
        if addr and addr not in identities and addr not in seen:
            seen.add(addr)
            to_list.append(addr)
    cc_list: list[str] = []
    for a in cc_addrs or []:
        addr = normalize_addr((a or {}).get("email") or "")
        if addr and addr not in identities and addr not in seen:
            seen.add(addr)
            cc_list.append(addr)
    return ", ".join(to_list), ", ".join(cc_list)


# Inline signature block at the end of the user's text: "-- \n<signature>". The form
# pre-fills it (before the quote); at send it's swapped for the rich HTML version if
# untouched, kept verbatim if edited, and NOT re-added if the user deleted it.
_INLINE_SIG_RE = re.compile(r"(?:\A|\n)-- ?\n(.*)\Z", re.S)


def _extract_inline_sig(before: str) -> tuple[str, str | None]:
    """Split the pre-quote text into (main text, inline signature or None)."""
    m = _INLINE_SIG_RE.search(before)
    if not m:
        return before, None
    return before[: m.start()].rstrip("\n"), m.group(1).strip()


def _split_quote(body: str) -> tuple[str, str]:
    """Split the composed body into (your text, quoted original) at the forwarded/replied
    block, so the signature can go BETWEEN them (before the quote), like normal clients.
    The first marker wins (nested quotes inside the original come later)."""
    cuts = []
    fi = body.find(_FWD_MARKER)
    if fi != -1:
        cuts.append(fi)
    m = _REPLY_QUOTE_RE.search(body)
    if m:
        cuts.append(m.start() + 1)  # keep the header's leading newline out of `before`
    if not cuts:
        return body, ""
    cut = min(cuts)
    return body[:cut].rstrip("\n"), body[cut:].lstrip("\n")


async def enabled_accounts(
    session: AsyncSession, space_id: uuid.UUID | None = None
) -> list[Account]:
    """Mailboxes one can send from — those of the open space, so a work reply is never
    offered a personal «From»."""
    q = select(Account).where(Account.enabled.is_(True)).order_by(Account.created_at)
    if space_id is not None:
        q = q.where(Account.space_id == space_id)
    return list((await session.execute(q)).scalars().all())


async def _account_for_email(session: AsyncSession, email_id: uuid.UUID) -> uuid.UUID | None:
    row = await session.execute(
        select(EmailOccurrence.account_id).where(EmailOccurrence.email_id == email_id).limit(1)
    )
    return row.scalar_one_or_none()


# Цитата без внешнего шрифта: `font-family` снаружи навязал бы оригиналу наш, а вся
# задача цитаты — остаться собой. Отбивка — как у почтовых клиентов.
_QUOTE_STYLE = "margin:0 0 0 .8ex;border-left:1px solid #ccc;padding-left:1ex"
# Встроенные картинки живут вложениями исходного письма; в исходящем их нет, и
# получатель увидел бы рамки с крестиком вместо логотипов в подписи.
_CID_IMG_RE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"\']cid:[^\"\']*[\"\'][^>]*>",
                         re.IGNORECASE)


def _strip_cid_images(html: str) -> str:
    return _CID_IMG_RE.sub("", html)


async def _quoted_original(
    session: AsyncSession, account: Account, forward_email_id, in_reply_to: str | None
) -> tuple[Email | None, str]:
    """Письмо, которое цитируем, и вид цитаты.

    Пересылка знает его по id, ответ — по Message-ID оригинала, который лежит в
    `in_reply_to`. Ящик в условии обязателен: один Message-ID живёт в нескольких
    ящиках, и без него можно процитировать чужое письмо."""
    if forward_email_id is not None:
        email = (
            await session.execute(select(Email).where(Email.id == forward_email_id))
        ).scalar_one_or_none()
        return email, "forward"
    if not in_reply_to:
        return None, "reply"
    email = (
        await session.execute(
            select(Email).where(
                Email.rfc_message_id == in_reply_to, Email.account_id == account.id
            )
        )
    ).scalars().first()
    return email, "reply"


def _quote_html(email: Email, kind: str, body: str) -> str:
    """HTML-цитата оригинала — или пусто, если её нельзя поставить честно.

    Пусто в двух случаях: у оригинала нет HTML (цитировать нечего, кроме текста) или
    человек правил цитату в композере. Правленую подменять нельзя: уйдёт не то, что
    он видел. Признак — текстовый блок, которым мы заполнили поле, больше не лежит в
    теле целиком."""
    if not email.body_raw_html:
        return ""
    if _quote_block(email, kind).strip() not in body:
        return ""
    inner = _strip_cid_images(htmlview.sanitize_email_html(email.body_raw_html))
    if not inner.strip():
        return ""
    who = email.from_name or email.from_addr
    when = email.sent_at.strftime("%d.%m.%Y %H:%M") if email.sent_at else "?"
    if kind == "forward":
        to = ", ".join(a.get("email", "") for a in (email.to_addrs or []) if a.get("email"))
        head = (f"{_h_text(_FWD_MARKER)}<br>От: {_h_text(who)} &lt;{_h_text(email.from_addr)}"
                f"&gt;<br>Дата: {_h_text(when)}<br>Кому: {_h_text(to)}"
                f"<br>Тема: {_h_text(email.subject or '')}")
    else:
        head = f"{_h_text(when)}, {_h_text(who)} писал(а):"
    return f'<div>{head}</div><blockquote style="{_QUOTE_STYLE}">{inner}</blockquote>'


def _h_text(t: str) -> str:
    """Экранирование без превращения переводов строк в <br> — для шапки цитаты."""
    return _html_escape(t or "")


async def reply_context(
    session: AsyncSession, email_id: uuid.UUID, all_: bool = False
) -> dict | None:
    email = (
        await session.execute(select(Email).where(Email.id == email_id))
    ).scalar_one_or_none()
    if email is None:
        return None
    subject = email.subject or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}".strip()
    references = list(email.reference_ids or [])
    if email.rfc_message_id and email.rfc_message_id not in references:
        references.append(email.rfc_message_id)
    to, cc = email.from_addr, ""
    if all_:
        idents = await get_identity_set(session)
        to, cc = _reply_all_recipients(email.from_addr, email.to_addrs, email.cc_addrs, idents)
        to = to or email.from_addr  # degenerate case: everyone filtered out
    return {
        "to": to,
        "cc": cc,
        "subject": subject,
        "in_reply_to": email.rfc_message_id,
        "references": references,
        "account_id": await _account_for_email(session, email_id),
        "thread_id": email.thread_id,
        "quote": email.body_text,
        "quoted": _quote_block(email, "reply"),
    }


async def forward_context(session: AsyncSession, email_id: uuid.UUID) -> dict | None:
    email = (
        await session.execute(select(Email).where(Email.id == email_id))
    ).scalar_one_or_none()
    if email is None:
        return None
    subject = email.subject or ""
    if not subject.lower().startswith(("fwd:", "fw:")):
        subject = f"Fwd: {subject}".strip()
    return {
        "to": "",
        "subject": subject,
        "in_reply_to": None,
        "references": [],
        "account_id": await _account_for_email(session, email_id),
        "thread_id": email.thread_id,
        "quote": email.body_text,
        "quoted": _quote_block(email, "forward"),
        "forward_email_id": str(email_id),
        # Что уедет вместе с письмом — видно сразу, а не выясняется отказом при отправке.
        "forward_files": await forward_files(session, email_id),
    }


async def forward_files(session: AsyncSession, email_id: uuid.UUID) -> list[dict]:
    """Вложения оригинала для показа в композере: имя и есть ли файл на этой стороне."""
    ready, missing, unknown = await attachments_svc.for_forwarding(session, email_id)
    files = [{"name": f["name"], "here": True} for f in ready]
    files += [{"name": name, "here": False} for name in missing]
    if unknown and not files:
        files = [{"name": "вложения оригинала", "here": False}]
    return files


async def forward_context_thread(session: AsyncSession, thread_id: uuid.UUID) -> dict | None:
    """Forward the latest message of a thread."""
    target = (
        await session.execute(
            select(Email)
            .where(Email.thread_id == thread_id)
            .order_by(Email.sent_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if target is None:
        return None
    return await forward_context(session, target.id)


async def reply_context_thread(
    session: AsyncSession, thread_id: uuid.UUID, all_: bool = False
) -> dict | None:
    """Reply to a thread: target the latest inbound message, else the latest message."""
    inbound = (
        await session.execute(
            select(Email)
            .where(Email.thread_id == thread_id, Email.direction == Direction.INBOUND)
            .order_by(Email.sent_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    target = inbound
    if target is None:
        target = (
            await session.execute(
                select(Email)
                .where(Email.thread_id == thread_id)
                .order_by(Email.sent_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if target is None:
        return None
    return await reply_context(session, target.id, all_=all_)


@dataclass(frozen=True)
class SendResult:
    error: str | None = None


async def _outgoing_files(
    session: AsyncSession, attachments: list[dict] | None, forward_email_id: uuid.UUID | None,
) -> tuple[list[dict], str | None]:
    """Файлы письма: приложенные в композере и, при пересылке, файлы оригинала — или отказ."""
    outgoing: list[dict] = list(attachments or [])
    if forward_email_id is None:
        return outgoing, None
    try:
        ready, missing, unknown = await attachments_svc.for_forwarding(session, forward_email_id)
    except Exception as exc:  # noqa: BLE001 — ящик недоступен: сказать, а не молчать
        return [], f"Не удалось взять вложения оригинала: {exc}"
    # Молча уйти без части содержимого нельзя: человек видел файлы в письме и
    # считает, что переслал их. Решение — за ним: убрать пересылку или дождаться.
    if unknown:
        return [], ("У оригинала есть вложения, но их нет на этом компьютере — "
                    "переслать письмо целиком не получится."
                    )
    return outgoing + ready, None


@dataclass(frozen=True)
class _Bodies:
    text: str
    html: str
    html_without_quote: str  # то же письмо, но цитата оригинала — текстом, а не его HTML


async def _bodies(
    session: AsyncSession, account: Account, body: str,
    forward_email_id: uuid.UUID | None, in_reply_to: str | None,
) -> _Bodies:
    """Текст и HTML письма с подписью и цитатой оригинала."""
    # Signature: the compose form pre-fills its TEXT form inline, between your text and
    # the quote ("-- \n…"), so the user SEES where it lands and can edit or delete it.
    # Here: untouched inline sig -> swapped for the rich HTML version in the HTML part;
    # edited -> the user's text wins verbatim; deleted (no "-- " block) -> no signature.
    sig_html = await signatures.get_signature(session, account.id)
    sig_text = htmlview.signature_to_text(sig_html)
    # HTML textareas submit CRLF, but _split_quote's reply-header regex assumes \n — with
    # CRLF the split silently failed and the signature dropped to the very bottom (after the
    # quoted original) instead of sitting right below your text. Normalize first.
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    before, quote = _split_quote(body)
    main, inline_sig = _extract_inline_sig(before)

    def _h(t: str) -> str:
        # Ряды пробелов держатся на &nbsp;: без pre-wrap HTML схлопнул бы отступы,
        # которыми человек выравнивал строки.
        escaped = _html_escape(t).replace("\n", "<br>")
        return re.sub(r"  +", lambda m: "&nbsp;" * (len(m.group()) - 1) + " ", escaped)

    if inline_sig is None:
        sig_text_block, sig_html_block = "", ""  # deleted by the user (or none set)
    elif inline_sig == sig_text.strip() and sig_html.strip():
        sig_text_block = f"\n\n-- \n{sig_text}"
        sig_html_block = f"<br>--<br>{sig_html}"  # untouched -> rich version survives
    else:
        sig_text_block = f"\n\n-- \n{inline_sig}"
        sig_html_block = f"<br>--<br>{_h(inline_sig)}"  # edited -> user's text wins

    text_parts = [main, sig_text_block]
    if quote:
        text_parts.append(f"\n\n{quote}")
    text_body = "".join(text_parts)

    body_style = _body_style(sig_html)
    original, quote_kind = await _quoted_original(
        session, account, forward_email_id, in_reply_to
    )
    # Цитату оригинала отдаём его собственным HTML: экранированный текст рассыпал
    # таблицы и терял шрифты — именно то, ради чего переписку и цитируют.
    rich_quote = _quote_html(original, quote_kind, body) if original is not None else ""

    def _assemble(rich: str) -> str:
        parts = [f'<div style="{body_style}">{_h(main)}</div>', sig_html_block]
        if rich:
            parts.append(f"<br><br>{rich}")
        elif quote:
            parts.append(f'<br><br><div style="{body_style}">{_h(quote)}</div>')
        return "".join(parts)

    return _Bodies(text_body, _assemble(rich_quote), _assemble(""))


async def send(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None,
    references: list[str],
    forward_email_id: uuid.UUID | None = None,
    attachments: list[dict] | None = None,
    cc: str = "",
) -> SendResult:
    """Send a message. See SendResult.

    `attachments` are user-attached files ({"name", "mime", "bytes"}) from the composer.
    When forward_email_id is given, the original message's attachments are also fetched
    (from cache/IMAP) and attached to the outgoing mail (forwarding).
    """
    account = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if account is None:
        return SendResult(error="Аккаунт не найден")
    try:
        password = resolve_password(account)
    except Exception as exc:  # noqa: BLE001
        return SendResult(error=f"Нет пароля для аккаунта: {exc}")
    outgoing, refusal = await _outgoing_files(session, attachments, forward_email_id)
    if refusal is not None:
        return SendResult(error=refusal)
    bodies = await _bodies(session, account, body, forward_email_id, in_reply_to)
    text_body, html_body = bodies.text, bodies.html
    try:
        msg = await send_mail(
            account, password, to=to, subject=subject, body=text_body,
            in_reply_to=in_reply_to, references=references, attachments=outgoing,
            html_body=html_body, cc=cc,
        )
    except Exception as exc:  # noqa: BLE001
        return SendResult(error=f"Ошибка отправки: {exc}")

    # Store a local copy of the outbound message right away (don't wait for the
    # next IMAP sync). It goes through the same dedup/threading/extraction path as
    # synced mail, so this email shows up in "Отправленные" and any commitment in
    # it ("я обещал …") is tracked. When the server's Sent copy later syncs, it
    # dedups by Message-ID and just adds provenance.
    try:
        cfg = get_app_config()
        identities = await get_identity_set(session)
        parsed = parse_raw_email(msg.as_bytes())
        await store_email(
            session, account_id=account_id, parsed=parsed, identities=identities,
            normalize_plus_tags=cfg.normalize_plus_tags,
        )
    except Exception:  # noqa: BLE001 — a local-copy failure must not fail the send
        log.exception("Failed to store local copy of sent message")

    session.add(
        AgentDecision(
            kind="send",
            payload={"account": account.name, "to": to, "subject": subject},
        )
    )
    return SendResult()
