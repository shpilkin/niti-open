"""MIME parsing: raw bytes -> ParsedEmail, using the stdlib email package."""

from __future__ import annotations

import email.policy
import re
from datetime import UTC, datetime
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

from email_reader.normalization.model import EmailAddress, ParsedEmail

_MSGID_RE = re.compile(r"<[^>]+>")

# compat32 hands us the RAW header value so we can decode it ourselves. The modern
# `default` policy, by contrast, keeps the whitespace that separates a pair of adjacent
# RFC 2047 encoded-words — violating RFC 2047 §6.2 and producing a spurious space inside
# a word when a name is split mid-word across encoded-words (e.g. "Владимиров ич").
_POLICY = email.policy.compat32


def _decode(value: object) -> str:
    """Decode a header value to text, handling both RFC 2047 encoded-words (concatenating
    adjacent ones WITHOUT the separating whitespace, per §6.2) and raw 8-bit UTF-8 headers
    (common from Russian mail servers)."""
    if value is None:
        return ""
    try:
        parts: list[str] = []
        for data, charset in decode_header(value):
            if isinstance(data, bytes):
                enc = charset or "utf-8"
                try:
                    parts.append(data.decode(enc, errors="replace"))
                except LookupError:
                    parts.append(data.decode("utf-8", errors="replace"))
            else:
                parts.append(data)
        return "".join(parts)
    except Exception:  # noqa: BLE001 — a malformed header must never crash ingestion
        return str(value)


def _addr(value: object) -> EmailAddress | None:
    for name, addr in getaddresses([_decode(value)]):
        if addr:
            return EmailAddress(name=_decode(name) or None, email=addr.strip().lower())
    return None


def _addrs(values: list[object]) -> list[EmailAddress]:
    out: list[EmailAddress] = []
    seen: set[str] = set()
    for name, addr in getaddresses([_decode(v) for v in values]):
        if addr:
            e = addr.strip().lower()
            if e in seen:
                continue
            seen.add(e)
            out.append(EmailAddress(name=_decode(name) or None, email=e))
    return out


def _message_ids(value: object) -> list[str]:
    return _MSGID_RE.findall(_decode(value))


def _own_parts(msg: Message):
    """Leaf parts belonging to THIS message.

    An attached letter (message/rfc822) is somebody else's body, and walk() descends into
    it. Worse, such a part usually arrives base64-encoded: the payload never parses into a
    message, so it surfaces as a part with no headers at all — content type defaults to
    text/plain, no Content-Disposition to skip on — and a screen of base64 replaces the
    real text of the letter.
    """
    if msg.get_content_maintype() == "message":
        return
    if msg.is_multipart():
        payload = msg.get_payload()
        if isinstance(payload, list):
            for part in payload:
                if isinstance(part, Message):
                    yield from _own_parts(part)
        return
    yield msg


def _body(msg: Message) -> tuple[str | None, str | None]:
    """Return (text, html). Prefers text/plain; keeps text/html for fallback."""
    text: str | None = None
    html: str | None = None
    if msg.is_multipart():
        for part in _own_parts(msg):
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            payload = _part_text(part)
            if payload is None:
                continue
            if ctype == "text/plain" and text is None:
                text = payload
            elif ctype == "text/html" and html is None:
                html = payload
    else:
        payload = _part_text(msg)
        if msg.get_content_type() == "text/html":
            html = payload
        else:
            text = payload
    return text, html


# The email package matches Content-Transfer-Encoding on the WHOLE value, so a header
# a broken sender folded into it («base64 риложения в аккаунте …») means the payload is
# never decoded and the body arrives as a screen of base64.
_CTE_TOKENS = frozenset({"base64", "quoted-printable", "7bit", "8bit", "binary"})


def _repair_encoding_header(part: Message) -> None:
    raw = str(part.get("Content-Transfer-Encoding") or "").strip()
    if not raw or raw.lower() in _CTE_TOKENS:
        return
    token = raw.split()[0].strip(";").lower()
    if token in _CTE_TOKENS:
        del part["Content-Transfer-Encoding"]
        part["Content-Transfer-Encoding"] = token


def _part_text(part: Message) -> str | None:
    _repair_encoding_header(part)
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return None
    if payload is None:
        return None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", errors="replace")


def _sent_at(msg: Message) -> datetime:
    raw = msg.get("Date")
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
        except (TypeError, ValueError):
            pass
    return datetime.now(UTC)


# Headers we keep verbatim for triage / debugging.
_KEEP_HEADERS = {
    "list-unsubscribe",
    "list-id",
    "precedence",
    "auto-submitted",
    "x-autoreply",
    "return-path",
}


def _has_attachments(msg) -> bool:
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()
        filename = part.get_filename()
        if disp == "attachment" or (filename and disp != "inline"):
            return True
    return False


def parse_raw_email(raw: bytes) -> ParsedEmail:
    msg = message_from_bytes(raw, policy=_POLICY)
    from_addr = _addr(msg.get("From")) or EmailAddress(email="unknown@unknown")
    text, html = _body(msg)

    kept_headers = {
        k.lower(): _decode(v) for k, v in msg.items() if k.lower() in _KEEP_HEADERS
    }

    message_id = None
    mids = _message_ids(msg.get("Message-ID"))
    if mids:
        message_id = mids[0]

    return ParsedEmail(
        rfc_message_id=message_id,
        **{"from": from_addr},
        to=_addrs([msg.get("To")]),
        cc=_addrs([msg.get("Cc")]),
        subject=_decode(msg.get("Subject")) or None,
        body_text=text,
        body_html=html,
        sent_at=_sent_at(msg),
        in_reply_to=(_message_ids(msg.get("In-Reply-To")) or [None])[0],
        references=_message_ids(msg.get("References")),
        headers=kept_headers,
        has_attachments=_has_attachments(msg),
    )
