"""Matching a person by name or e-mail across sources.

Extracted from the calendar prep gate when that gate was removed: the person dossier
(`views.person_dossier`) matches an item's counterparty against a name typed by the user,
and that has nothing to do with calendars. Exact token overlap only — no substring fuzz,
so «Аня» never matches «Анна» by accident and Ann never matches Johanna.
"""

from __future__ import annotations

import re

from email_reader.normalization.identity import normalize_addr

# Tokens shorter than this are too ambiguous to match on (Ira/Kira, Ann/Johanna).
_TOKEN_MIN = 3
_TOKEN_RE = re.compile(r"[^\w@.+-]+", re.UNICODE)


def _tokens(s: str) -> set[str]:
    return {w for w in _TOKEN_RE.split((s or "").lower()) if len(w) >= _TOKEN_MIN}


def name_tokens(name: str) -> set[str]:
    """The words of a name that are long enough to match on."""
    return _tokens(name)


def attendee_terms(att: dict) -> set[str]:
    """Match tokens for one person given as {"email": …} / {"name": …}: the normalized
    address plus the words of the name."""
    terms: set[str] = set()
    email = (att.get("email") or "").strip()
    if email:
        terms.add(normalize_addr(email))
    terms |= _tokens(att.get("name") or "")
    return terms


def matches_counterparty(counterparty: str | None, terms: set[str]) -> bool:
    """Word-level match of an item counterparty (name-or-email) against those terms."""
    cp = (counterparty or "").strip().lower()
    if not cp or not terms:
        return False
    if cp in terms:  # exact email or exact single-word name
        return True
    return bool(_tokens(cp) & terms)
