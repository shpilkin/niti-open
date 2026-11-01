"""VIP / important contacts — a deterministic signal for board priority.

A small, global (not per-mailbox) list of addresses and domains that mark a meeting
participant as "important" (management, key clients, board...). Stored as free text in
``app_settings`` under ``vip:contacts`` (one entry per line), mirroring profiles.py.
Parsed on read into normalized address/domain entries. Lives in the "Метки и правила"
settings section next to sender rules — both are correspondent → behaviour rules.

Used by the gate (rule 2): any attendee whose email matches -> the meeting needs prep.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import settings_store
from email_reader.normalization.identity import normalize_addr

_KEY = "vip:contacts"

PLACEHOLDER = (
    "ceo@acme.ru\n"
    "petrov@acme.ru\n"
    "acme.ru\n"
    "Один адрес или домен в строке. Домен (без @) делает VIP всех с этого домена."
)


def _domain_of(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower().strip() if "@" in addr else ""


def parse(text: str) -> list[dict]:
    """Split raw text into normalized entries: {"type": "address"|"domain", "pattern": str}."""
    out: list[dict] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        raw = line.strip()
        if not raw or " " in raw and "@" not in raw:
            # Skip blank lines and the free-text hint line (has spaces, no address).
            continue
        raw = raw.lstrip("@")
        if not raw:
            continue
        if "@" in raw:
            pattern, mtype = normalize_addr(raw), "address"
        else:
            pattern, mtype = raw.lower(), "domain"
        key = f"{mtype}:{pattern}"
        if pattern and key not in seen:
            seen.add(key)
            out.append({"type": mtype, "pattern": pattern})
    return out


def is_vip(email: str, entries: list[dict]) -> bool:
    """True if the (normalized) email matches a VIP address or domain entry."""
    if not email or not entries:
        return False
    addr = normalize_addr(email)
    dom = _domain_of(addr)
    for e in entries:
        if e["type"] == "address" and e["pattern"] == addr:
            return True
        if e["type"] == "domain" and e["pattern"] == dom:
            return True
    return False


async def get_raw(session: AsyncSession) -> str:
    return (await settings_store.get_value(session, _KEY)) or ""


async def set_raw(session: AsyncSession, text: str) -> None:
    await settings_store.set_value(session, _KEY, (text or "").strip())


async def get_entries(session: AsyncSession) -> list[dict]:
    return parse(await get_raw(session))


async def add_entry(session: AsyncSession, email: str) -> None:
    """Append one address to the raw list (used by the suggestion chips)."""
    addr = normalize_addr(email)
    if not addr:
        return
    if is_vip(addr, await get_entries(session)):
        return  # already covered by an address or domain entry
    raw = await get_raw(session)
    await set_raw(session, f"{raw}\n{addr}" if raw else addr)


async def suggestions(session: AsyncSession, limit: int = 6) -> list[dict]:
    """Frequent human senders that are not yet VIP — candidates for the list.

    Deterministic: top inbound correspondents by message count, excluding bulk mail
    (is_low_value), my own identities, and anyone already matched by the VIP list."""
    from sqlalchemy import func, select

    from email_reader.identities import get_identity_set
    from email_reader.models import Email

    entries = await get_entries(session)
    idents = await get_identity_set(session)
    rows = (
        await session.execute(
            select(Email.from_addr, func.max(Email.from_name), func.count(Email.id))
            .where(Email.direction == "inbound", Email.is_low_value.is_(False))
            .group_by(Email.from_addr)
            .order_by(func.count(Email.id).desc())
            .limit(50)
        )
    ).all()
    out: list[dict] = []
    for addr, name, cnt in rows:
        norm = normalize_addr(addr or "")
        if not norm or norm in idents or is_vip(norm, entries):
            continue
        out.append({"email": norm, "name": name, "count": cnt})
        if len(out) >= limit:
            break
    return out
