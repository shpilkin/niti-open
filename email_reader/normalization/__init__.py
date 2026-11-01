"""Normalization layer: raw email -> Unified Email Model, identity/direction, dedup."""

from email_reader.normalization.dedup import compute_dedup_hash, normalize_subject
from email_reader.normalization.identity import normalize_addr, resolve_direction
from email_reader.normalization.model import EmailAddress, ParsedEmail
from email_reader.normalization.parser import parse_raw_email

__all__ = [
    "EmailAddress",
    "ParsedEmail",
    "parse_raw_email",
    "normalize_addr",
    "resolve_direction",
    "compute_dedup_hash",
    "normalize_subject",
]
