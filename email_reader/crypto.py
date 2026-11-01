"""Symmetric encryption for mailbox app-passwords at rest (Fernet).

The key lives outside the database, the ciphertext inside it. On a compromised
host both are reachable — this protects against DB-only leaks (backups, dumps,
replicas), which is the realistic threat for a personal tool.

Key resolution order:
1. ``ENCRYPTION_KEY`` from the environment/.env (explicit, e.g. server topology).
2. A persisted ``encryption.key`` file in the data dir, auto-generated on first
   use (mode 0600). This makes the desktop app work out of the box without the
   user hand-editing a .env, while keeping the key out of the DB file itself.
"""

from __future__ import annotations

import logging
import os
import stat
from functools import lru_cache

from cryptography.fernet import Fernet

from email_reader.config import get_settings
from email_reader.paths import get_data_dir

log = logging.getLogger("email_reader.crypto")


def _load_or_create_key() -> bytes:
    key = get_settings().encryption_key
    if key:
        return key.encode() if isinstance(key, str) else key

    key_path = get_data_dir() / "encryption.key"
    if key_path.exists():
        return key_path.read_bytes().strip()

    new_key = Fernet.generate_key()
    # Write with owner-only permissions before anything sensitive is encrypted.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, new_key)
    finally:
        os.close(fd)
    log.info("Generated encryption key at %s", key_path)
    return new_key


@lru_cache
def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
