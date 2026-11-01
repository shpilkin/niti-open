"""Portable column types that behave the same on SQLite and Postgres.

SQLite has no native timezone support: SQLAlchemy's ``DateTime`` strips tzinfo on
write and returns *naive* datetimes on read. The rest of the codebase works in
timezone-aware UTC (``datetime.now(UTC)``), so a naive value coming back from the
DB would blow up any comparison with ``TypeError: can't compare offset-naive and
offset-aware datetimes``.

``UTCDateTime`` closes that gap: it normalises every value to UTC on the way in
and re-attaches UTC on the way out, so the application only ever sees tz-aware
UTC datetimes regardless of dialect.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Timezone-aware DateTime that always stores/returns UTC."""

    impl = DateTime
    cache_ok = True

    def __init__(self, *args, **kwargs):
        # Always timezone-aware; tolerate alembic re-rendering ``timezone=True``.
        kwargs.setdefault("timezone", True)
        super().__init__(*args, **kwargs)

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            # Assume naive inputs are already UTC.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
