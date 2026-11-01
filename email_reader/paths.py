"""Filesystem locations for data and bundled resources.

Two distinct roots, on purpose:

- **Data dir** (writable): SQLite DB, ``config.yaml``, ``.env``. In a packaged
  ``.app`` this lives under ``~/Library/Application Support/EmailReader`` so the
  user never edits files inside the bundle. In development it defaults to the
  project root, so the existing dev/test workflow is unchanged.
- **Resource dir** (read-only): ``templates/``, ``static/``, ``alembic/``.
  When frozen by PyInstaller these are unpacked under ``sys._MEIPASS``; in
  development they sit next to the source tree.

Override the data dir explicitly with the ``EMAIL_READER_DATA_DIR`` env var --
the macOS shell sets this when launching the bundled backend.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def get_data_dir() -> Path:
    """Writable directory for the DB, config.yaml and .env. Created if missing."""
    env = os.environ.get("EMAIL_READER_DATA_DIR")
    if env:
        data_dir = Path(env).expanduser()
    elif is_frozen():
        data_dir = Path.home() / "Library" / "Application Support" / "EmailReader"
    else:
        data_dir = PROJECT_ROOT
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_resource_dir() -> Path:
    """Root for read-only bundled assets (templates/, static/, alembic/)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return PROJECT_ROOT


def resource_path(*parts: str) -> Path:
    """Path to a bundled read-only resource, e.g. ``resource_path("templates")``."""
    return get_resource_dir().joinpath(*parts)
