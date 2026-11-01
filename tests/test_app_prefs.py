from __future__ import annotations

from email_reader import app_prefs


async def test_extract_backfill_default_is_config(db):
    async with db() as s:
        # No override -> config.yaml default (30 ≈ 1 month).
        assert await app_prefs.get_extract_backfill_days(s) == 30


async def test_extract_backfill_override(db):
    async with db() as s:
        await app_prefs.set_extract_backfill_days(s, 14)
        await s.commit()
        assert await app_prefs.get_extract_backfill_days(s) == 14
        # 0 = no limit is a valid, distinct value (not treated as "unset").
        await app_prefs.set_extract_backfill_days(s, 0)
        await s.commit()
        assert await app_prefs.get_extract_backfill_days(s) == 0
        # Negative clamps to 0.
        await app_prefs.set_extract_backfill_days(s, -5)
        await s.commit()
        assert await app_prefs.get_extract_backfill_days(s) == 0
