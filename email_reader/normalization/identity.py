"""Identity resolution: which addresses are 'me', and the direction of a message.

Direction is computed against the identity set, NOT folders (Inbox/Sent lie:
self-CC, mail-to-self, cross-account copies).
"""

from __future__ import annotations

from email_reader.enums import Direction


def normalize_addr(addr: str, normalize_plus_tags: bool = False) -> str:
    """Lowercase + trim. Optionally strip Gmail-style +tags and dots in the local part.

    Plus-tag normalization is off by default: aggressive normalization can collapse
    distinct external addresses, which would corrupt direction/dedup logic.
    """
    addr = (addr or "").strip().lower()
    if not normalize_plus_tags or "@" not in addr:
        return addr
    local, _, domain = addr.partition("@")
    local = local.split("+", 1)[0]
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.replace(".", "")
    return f"{local}@{domain}"


def resolve_direction(
    from_addr: str,
    recipient_addrs: list[str],
    identities: set[str],
    normalize_plus_tags: bool = False,
) -> Direction:
    """Classify a message relative to the user's identities.

    - outbound: From is me, and there is at least one external recipient.
    - self:     From is me and every recipient is also me (mail to self).
    - inbound:  From is not me.
    """
    norm = lambda a: normalize_addr(a, normalize_plus_tags)  # noqa: E731
    ids = {norm(i) for i in identities}
    from_is_me = norm(from_addr) in ids
    recipients = {norm(a) for a in recipient_addrs if a}

    if not from_is_me:
        return Direction.INBOUND
    if recipients and recipients <= ids:
        return Direction.SELF
    return Direction.OUTBOUND


def is_me(addr: str, identities: set[str], normalize_plus_tags: bool = False) -> bool:
    ids = {normalize_addr(i, normalize_plus_tags) for i in identities}
    return normalize_addr(addr, normalize_plus_tags) in ids
