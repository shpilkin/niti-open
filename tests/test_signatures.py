from __future__ import annotations

import uuid

from email_reader import signatures


async def test_default_empty(db):
    async with db() as s:
        assert await signatures.get_signature(s, uuid.uuid4()) == ""
        assert await signatures.get_signature(s, None) == ""


async def test_set_and_get_is_per_account(db):
    a, b = uuid.uuid4(), uuid.uuid4()
    async with db() as s:
        await signatures.set_signature(s, a, "—\nГлеб Миронов")
        await s.commit()
        assert await signatures.get_signature(s, a) == "—\nГлеб Миронов"
        # Another mailbox is unaffected (isolation).
        assert await signatures.get_signature(s, b) == ""


async def test_for_accounts_maps_string_ids(db):
    a = uuid.uuid4()
    acc = type("A", (), {"id": a})
    async with db() as s:
        await signatures.set_signature(s, a, "sig")
        await s.commit()
        m = await signatures.for_accounts(s, [acc])
        assert m == {str(a): "sig"}
