"""The bearer token that guards the MCP endpoint.

What it does and does not buy, plainly: the web UI on 127.0.0.1 has no authentication
either, so the token is not the difference between locked and open. It buys two things.
Any other program on this machine can reach localhost, and the write tools (send a
letter, close a commitment) should not be one `curl` away for all of them. And a required
header cannot be forged by a form on some page in the browser — which is the one way a
web page could otherwise drive a localhost API.

Stored as a SHA-256 digest: the plain token is shown once, at the moment it is made.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import settings_store

TOKEN_KEY = "mcp.token_sha256"
ENABLED_KEY = "mcp.enabled"


def digest(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


async def is_enabled(session: AsyncSession) -> bool:
    return (await settings_store.get_value(session, ENABLED_KEY)) == "1"


async def set_enabled(session: AsyncSession, enabled: bool) -> None:
    await settings_store.set_value(session, ENABLED_KEY, "1" if enabled else "0")


async def has_token(session: AsyncSession) -> bool:
    return bool(await settings_store.get_value(session, TOKEN_KEY))


async def issue(session: AsyncSession) -> str:
    """Make a new token, store its digest, return the token itself — once."""
    token = new_token()
    await settings_store.set_value(session, TOKEN_KEY, digest(token), secret=True)
    return token


async def revoke(session: AsyncSession) -> None:
    await settings_store.delete_value(session, TOKEN_KEY)


async def check(session: AsyncSession, header: str | None) -> bool:
    """True when the Authorization header carries the current token."""
    stored = await settings_store.get_value(session, TOKEN_KEY)
    if not stored or not header:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return False
    return hmac.compare_digest(stored, digest(value))
