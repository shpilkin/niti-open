"""On-demand email attachments.

Attachments aren't stored at sync time (that would bloat the DB). Instead, when a
message is opened we re-fetch its raw bytes from IMAP by UID, extract attachment
parts, and cache them on disk under the data dir. Subsequent opens read the cache,
so each message is fetched at most once.

Cache layout:  <data_dir>/attachments/<email_id>/
    _index.json   [{"name","mime","size","file"}, ...]
    0, 1, 2, ...   raw attachment bytes (referenced by "file")
"""

from __future__ import annotations

import base64
import binascii
import email.policy
import json
import mimetypes
import re
import uuid
from datetime import datetime, timedelta
from email import message_from_bytes
from email.message import Message
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import htmlview
from email_reader.accounts import get_account, resolve_password
from email_reader.connectors.imap import fetch_raw
from email_reader.models import Email, EmailOccurrence
from email_reader.paths import get_data_dir

_UNSAFE = re.compile(r"[^\w.\- ()\[\]]+", re.UNICODE)


def _safe_name(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1].strip()  # strip any path
    name = _UNSAFE.sub("_", name)
    return name[:120] or "attachment"


def _cache_dir(email_id: uuid.UUID) -> Path:
    return get_data_dir() / "attachments" / str(email_id)


def adopt_cache(src: uuid.UUID, dst: uuid.UUID) -> bool:
    """Отдать файлы одного письма другому — при слиянии двух копий одного письма.
    Свои файлы у получателя важнее: их не трогаем."""
    source, target = _cache_dir(src), _cache_dir(dst)
    if not source.exists() or target.exists():
        return False
    source.rename(target)
    return True


def _attached_letter(part: Message) -> bytes:
    """An attached letter as file bytes.

    The email package never decodes a message/rfc822 part — get_payload(decode=True)
    returns None — and when the part arrives base64-encoded the payload does not parse
    into a message either, so what comes back is the base64 itself."""
    payload = part.get_payload()
    body = payload[0].as_bytes() if isinstance(payload, list) and payload else b""
    if str(part.get("Content-Transfer-Encoding") or "").strip().lower() == "base64":
        try:
            body = base64.b64decode(re.sub(rb"\s+", b"", body), validate=True)
        except (binascii.Error, ValueError):
            pass  # not base64 after all — keep what we have rather than lose the letter
    return body


def _letter_name(part: Message, body: bytes) -> str:
    if part.get_filename():
        return _safe_name(part.get_filename())
    subject = str(message_from_bytes(body, policy=email.policy.default).get("Subject") or "")
    return _safe_name(subject.strip() or "письмо")[:60] + ".eml"


def parse_attachments(raw: bytes) -> list[dict]:
    """Extract real attachments from raw MIME (skips inline body parts)."""
    msg = message_from_bytes(raw, policy=email.policy.default)
    out: list[dict] = []
    for part in msg.walk():
        # A letter attached to a letter is a file like any other. It is not a body part —
        # normalization.parser refuses to read it as one — so if it is not offered here it
        # is not offered anywhere.
        if part.get_content_maintype() == "message":
            body = _attached_letter(part)
            if body:
                out.append(
                    {"name": _letter_name(part, body), "mime": "message/rfc822", "bytes": body}
                )
            continue
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()  # 'attachment' | 'inline' | None
        filename = part.get_filename()
        if disp != "attachment" and not (filename and disp != "inline"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            payload = None
        if not payload:
            continue
        out.append(
            {
                "name": _safe_name(filename or f"attachment-{len(out) + 1}"),
                "mime": part.get_content_type(),
                "bytes": payload,
            }
        )
    return out


# On-demand fetches use a short timeout so an unreachable server fails fast and the
# UI can show "couldn't fetch" instead of spinning (sync uses the longer default).
_ONDEMAND_TIMEOUT = 10


def parse_inline_images(raw: bytes) -> list[dict]:
    """Картинки, вложенные в само письмо (<img src="cid:…">): Content-ID, тип, байты.

    parse_attachments их пропускает: это части тела письма, а не файлы."""
    msg = message_from_bytes(raw, policy=email.policy.default)
    out: list[dict] = []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_maintype() != "image":
            continue
        cid = (part.get("Content-ID") or "").strip().strip("<>").strip()
        if not cid:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            payload = None
        if payload:
            out.append({"cid": cid, "mime": part.get_content_type(), "bytes": payload})
    return out


def _store_inline(email_id: uuid.UUID, raw: bytes) -> list[dict]:
    """Вложенные картинки — рядом с файлами письма, чтобы не спрашивать сервер снова."""
    cache = _cache_dir(email_id)
    cache.mkdir(parents=True, exist_ok=True)
    meta: list[dict] = []
    for i, part in enumerate(parse_inline_images(raw)):
        (cache / f"inline-{i}").write_bytes(part["bytes"])
        meta.append({"cid": part["cid"], "mime": part["mime"], "file": f"inline-{i}"})
    (cache / "_inline.json").write_text(json.dumps(meta, ensure_ascii=False))
    return meta


async def _fetch_raw_for(session: AsyncSession, email_id: uuid.UUID) -> bytes | None:
    """Письмо целиком с IMAP-сервера, или None — сервер недоступен или ящик не IMAP."""
    occ = (
        await session.execute(
            select(EmailOccurrence).where(EmailOccurrence.email_id == email_id).limit(1)
        )
    ).scalar_one_or_none()
    if occ is None:
        return None
    account = await get_account(session, occ.account_id)
    if account is None or not account.enabled:
        return None
    try:
        return await fetch_raw(
            host=account.host, port=account.port, login=account.login,
            password=resolve_password(account), folder=occ.folder, uid=occ.uid,
            imap_timeout=_ONDEMAND_TIMEOUT,
        ) or None
    except Exception:  # noqa: BLE001
        return None


# Ящик, чей сервер не ответил за картинками письма, столько не спрашиваем снова: без сети
# каждое открытие ждало бы таймаут, прежде чем показать текст.
_INLINE_RETRY = timedelta(minutes=10)
_inline_unreachable: dict[uuid.UUID, datetime] = {}


async def inline_images(session: AsyncSession, email: Email) -> dict[str, str]:
    """Вложенные картинки письма: Content-ID → data:-адрес. Только те, что нашлись.

    Из IMAP-письма их вынимаем при первом открытии и кладём рядом с его файлами."""
    refs = htmlview.cid_refs(email.body_raw_html)
    if not refs:
        return {}
    cache = _cache_dir(email.id)
    index = cache / "_inline.json"
    if index.exists():
        meta = json.loads(index.read_text())
    else:
        key = email.account_id
        if key in _inline_unreachable and datetime.now() < _inline_unreachable[key]:
            return {}
        raw = await _fetch_raw_for(session, email.id)
        if raw is None:
            _inline_unreachable[key] = datetime.now() + _INLINE_RETRY
            return {}
        _inline_unreachable.pop(key, None)
        meta = _store_inline(email.id, raw)
    by_cid = {m["cid"]: m for m in meta}
    found = {ref: (by_cid[ref]["mime"], ref, cache / by_cid[ref]["file"])
             for ref in refs if ref in by_cid}
    return _data_urls(found)


def _data_urls(found: dict[str, tuple[str, str, Path]]) -> dict[str, str]:
    """ref → (тип, имя, файл) в ref → data:-адрес; только картинки, что лежат на диске."""
    out: dict[str, str] = {}
    for ref, (mime, name, path) in found.items():
        if not mime.startswith("image/"):
            mime = mimetypes.guess_type(name)[0] or mime
        if mime.startswith("image/") and path.exists():
            out[ref] = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"
    return out


async def fetch_attachments(
    session: AsyncSession, email_id: uuid.UUID
) -> tuple[list[dict], str]:
    """Cached attachment metadata + a status: 'ok' (fetched/cached, list may be empty)
    or 'unavailable' (couldn't reach the mailbox — retry later)."""
    cache = _cache_dir(email_id)
    index = cache / "_index.json"
    if index.exists():
        return json.loads(index.read_text()), "ok"

    occ = (
        await session.execute(
            select(EmailOccurrence).where(EmailOccurrence.email_id == email_id).limit(1)
        )
    ).scalar_one_or_none()
    if occ is None:
        return [], "unavailable"
    account = await get_account(session, occ.account_id)
    if account is None or not account.enabled:
        return [], "unavailable"
    try:
        password = resolve_password(account)
        raw = await fetch_raw(
            host=account.host, port=account.port, login=account.login,
            password=password, folder=occ.folder, uid=occ.uid,
            imap_timeout=_ONDEMAND_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        return [], "unavailable"
    if not raw:
        return [], "unavailable"

    parts = parse_attachments(raw)
    cache.mkdir(parents=True, exist_ok=True)
    if not (cache / "_inline.json").exists():
        _store_inline(email_id, raw)  # то же письмо понадобится для его картинок
    meta: list[dict] = []
    for i, p in enumerate(parts):
        (cache / str(i)).write_bytes(p["bytes"])
        meta.append({"name": p["name"], "mime": p["mime"], "size": len(p["bytes"]), "file": str(i)})
    index.write_text(json.dumps(meta, ensure_ascii=False))
    return meta, "ok"


async def for_forwarding(
    session: AsyncSession, email_id: uuid.UUID
) -> tuple[list[dict], list[str], bool]:
    """Файлы оригинала для пересылки, имена недостающих и признак «даже имён не знаем».

    Пересылать письмо без части его содержимого молча нельзя: человек видел вложения в
    письме и считает, что переслал их вместе с ним.
    """
    meta, status = await fetch_attachments(session, email_id)
    if status != "ok":
        email = await session.get(Email, email_id)
        return [], [], bool(email is not None and email.has_attachments)
    ready = read_cached_attachments(email_id)
    return ready, [m["name"] for m in meta if not m.get("file")], False


async def ensure_attachments(session: AsyncSession, email_id: uuid.UUID) -> list[dict]:
    """Cached attachment metadata, fetching + caching from IMAP on first call. Returns
    [] when the message can't be fetched (so it retries later) or has none."""
    meta, _status = await fetch_attachments(session, email_id)
    return meta


def read_cached_attachments(email_id: uuid.UUID) -> list[dict]:
    """Read cached attachments as [{name, mime, bytes}] (for forwarding). Empty if none."""
    cache = _cache_dir(email_id)
    index = cache / "_index.json"
    if not index.exists():
        return []
    out: list[dict] = []
    for m in json.loads(index.read_text()):
        if not m.get("file"):
            continue  # байтов на этом компьютере ещё нет
        path = cache / m["file"]
        if path.exists():
            out.append({"name": m["name"], "mime": m["mime"], "bytes": path.read_bytes()})
    return out


def attachment_file(email_id: uuid.UUID, idx: int) -> tuple[Path, dict] | None:
    """Resolve a cached attachment by index -> (path, metadata) or None."""
    cache = _cache_dir(email_id)
    index = cache / "_index.json"
    if not index.exists():
        return None
    meta = json.loads(index.read_text())
    if idx < 0 or idx >= len(meta):
        return None
    m = meta[idx]
    if not m.get("file"):
        return None  # байтов на этом компьютере ещё нет
    path = cache / m["file"]
    if not path.exists():
        return None
    return path, m
