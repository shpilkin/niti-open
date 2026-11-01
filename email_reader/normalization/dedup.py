"""Dedup keys and subject normalization.

Primary dedup key is the RFC Message-ID. When it's missing/broken we fall back to a
content hash. Subject normalization strips reply/forward prefixes (RU + EN) for the
threading fallback and for the content hash.
"""

from __future__ import annotations

import hashlib
import re

# Re:, Fwd:, Ответ:, Пересл:, RE[2]:, etc. — repeated at the start of a subject.
_PREFIX_RE = re.compile(
    r"^\s*(re|re\[\d+\]|fw|fwd|ответ|отв|пересл|пересылка)\s*:\s*",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


def normalize_subject(subject: str | None) -> str:
    s = subject or ""
    prev = None
    while prev != s:
        prev = s
        s = _PREFIX_RE.sub("", s)
    return _WS_RE.sub(" ", s).strip().lower()


def compute_dedup_hash(
    from_addr: str,
    subject: str | None,
    sent_at_iso: str,
    body_text: str | None,
) -> str:
    """Fallback dedup key when Message-ID is absent. Stable across mailboxes."""
    body = (body_text or "")[:2000]
    material = "\x1f".join(
        [
            (from_addr or "").strip().lower(),
            normalize_subject(subject),
            sent_at_iso,
            body,
        ]
    )
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()
