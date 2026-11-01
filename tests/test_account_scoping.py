"""Per-mailbox dedup + threading (a mailbox is a source), and per-space model resolution
(a space is the boundary, docs/spaces.md): a space's model only ever sees its space."""

from __future__ import annotations

from datetime import UTC, datetime

from email_reader import model_connections as mc
from email_reader.crypto import encrypt
from email_reader.ingest import store_email
from email_reader.models import Account, ModelConnection, Space, Thread
from email_reader.normalization import EmailAddress, ParsedEmail
from email_reader.pipeline.threading import assign_thread
from tests.conftest import SPACE_ID, mk_space


def _parsed(msgid: str | None, subject: str, body: str = "b") -> ParsedEmail:
    return ParsedEmail(
        **{
            "rfc_message_id": msgid,
            "from": EmailAddress(email="bob@x.com", name="Bob"),
            "to": [],
            "subject": subject,
            "body_text": body,
            "sent_at": datetime(2026, 6, 30, 12, 0, tzinfo=UTC),
        }
    )


async def _acc(s, name: str, login: str, space_id=SPACE_ID) -> Account:
    a = Account(space_id=space_id, name=name, provider="imap", host="h", port=993, login=login,
                password_enc="x", folders=["INBOX"], enabled=True)
    s.add(a)
    await s.flush()
    return a


async def test_dedup_is_per_account(db):
    async with db() as s:
        await mk_space(s)
        a1 = await _acc(s, "Работа", "w@y.ru")
        a2 = await _acc(s, "Дом", "h@g.com")
        p = _parsed("<same@x>", "Привет")
        e1, new1 = await store_email(s, account_id=a1.id, parsed=p, identities=set(),
                                     folder="INBOX", uid=1)
        e2, new2 = await store_email(s, account_id=a2.id, parsed=p, identities=set(),
                                     folder="INBOX", uid=1)
        # Same Message-ID in two mailboxes -> two separate emails (no commingling).
        assert new1 and new2
        assert e1.id != e2.id
        assert e1.account_id == a1.id and e2.account_id == a2.id
        # Same message again in account 1 -> deduped to the existing row.
        e1b, new1b = await store_email(s, account_id=a1.id, parsed=p, identities=set(),
                                       folder="OTHER", uid=2)
        assert not new1b and e1b.id == e1.id


async def test_threads_are_account_scoped(db):
    async with db() as s:
        await mk_space(s)
        a1 = await _acc(s, "Работа", "w@y.ru")
        a2 = await _acc(s, "Дом", "h@g.com")
        e1, _ = await store_email(s, account_id=a1.id, parsed=_parsed("<m1@x>", "Договор"),
                                  identities=set(), folder="INBOX", uid=1)
        e2, _ = await store_email(s, account_id=a2.id, parsed=_parsed("<m2@x>", "Договор"),
                                  identities=set(), folder="INBOX", uid=1)
        t1 = await assign_thread(s, e1)
        t2 = await assign_thread(s, e2)
        # Same normalized subject but different mailboxes -> separate threads.
        assert t1.id != t2.id
        assert t1.account_id == a1.id and t2.account_id == a2.id


async def test_resolve_model_per_space_and_thread(db):
    """Rewritten for spaces: the override moved from the mailbox to its space."""
    async with db() as s:
        await mk_space(s)
        home_space = await mk_space(s, 2)
        default = ModelConnection(name="Default", provider="anthropic", kind="anthropic",
                                  model="claude-x", api_key_enc=encrypt("dk"),
                                  is_default=True, enabled=True)
        work = ModelConnection(name="WorkGPT", provider="openai_custom", kind="openai",
                               base_url="http://x", model="gpt-work",
                               api_key_enc=encrypt("wk"), is_default=False, enabled=True)
        s.add_all([default, work])
        await s.flush()

        a_work = await _acc(s, "Работа", "w@y.ru")
        a_home = await _acc(s, "Дом", "h@g.com", space_id=home_space.id)
        (await s.get(Space, SPACE_ID)).model_connection_id = work.id  # override on the space
        home_space.model_connection_id = None                           # default
        await s.flush()

        rm_work = await mc.resolve_for_account(s, a_work.id)
        rm_home = await mc.resolve_for_account(s, a_home.id)
        assert rm_work.model == "gpt-work"
        assert rm_home.model == "claude-x"  # falls back to the global default

        # resolve_for_thread routes via the thread's owning account.
        tw = Thread(account_id=a_work.id)
        s.add(tw)
        await s.flush()
        rm_thread = await mc.resolve_for_thread(s, tw.id)
        assert rm_thread.model == "gpt-work"


async def test_disabled_account_model_falls_back_to_default(db):
    async with db() as s:
        await mk_space(s)
        default = ModelConnection(name="Default", provider="anthropic", kind="anthropic",
                                  model="claude-x", api_key_enc=encrypt("dk"),
                                  is_default=True, enabled=True)
        broken = ModelConnection(name="Off", provider="openai_custom", kind="openai",
                                 base_url="http://x", model="gpt-off",
                                 api_key_enc=encrypt("k"), is_default=False, enabled=False)
        s.add_all([default, broken])
        await s.flush()
        a = await _acc(s, "Работа", "w@y.ru")
        (await s.get(Space, SPACE_ID)).model_connection_id = broken.id  # disabled -> unusable
        await s.flush()
        rm = await mc.resolve_for_account(s, a.id)
        assert rm.model == "claude-x"
