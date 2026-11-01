"""Triage gate: cheap deterministic filter for bulk/automated mail.

low_value messages skip expensive LLM extraction. Decision + reason are returned so
they can be logged. This runs BEFORE the AI layer (cost + noise control).
"""

from __future__ import annotations

import re

_BULK_LOCALPARTS = re.compile(
    r"^(no-?reply|do-?not-?reply|mailer|mailer-daemon|bounce|notifications?|"
    r"newsletter|news|info|support|robot|auto|noreply)\b",
    re.IGNORECASE,
)
_BULK_PRECEDENCE = {"bulk", "list", "junk", "auto_reply"}


def triage(headers: dict[str, str], from_addr: str) -> tuple[bool, str | None]:
    """Return (is_low_value, reason)."""
    h = {k.lower(): (v or "") for k, v in (headers or {}).items()}

    if "list-unsubscribe" in h or "list-id" in h:
        return True, "list header (newsletter/list mail)"

    precedence = h.get("precedence", "").strip().lower()
    if precedence in _BULK_PRECEDENCE:
        return True, f"Precedence: {precedence}"

    auto = h.get("auto-submitted", "").strip().lower()
    if auto and auto != "no":
        return True, f"Auto-Submitted: {auto}"

    if "x-autoreply" in h:
        return True, "X-Autoreply"

    local = (from_addr or "").split("@", 1)[0]
    if _BULK_LOCALPARTS.match(local):
        return True, f"bulk sender local-part: {local}"

    return False, None
