"""Deterministic type gates before/after LLM extraction (engine._thread_flags/_allowed_types).

The rules: NEED_MY_REPLY / TO_DELEGATE require me in To of an inbound email (CC does not
count); MY_COMMITMENT / WAITING_FOR_REPLY / DELEGATED_TASK require my outbound email in
the thread. A thread with neither ("observer thread") skips the LLM entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime

from email_reader.commitment.engine import _allowed_types, _thread_flags
from email_reader.enums import ActionType
from email_reader.models import Email

ME = {"me@ya.ru"}


def _email(direction: str, to=None, cc=None) -> Email:
    return Email(
        direction=direction,
        from_addr="someone@x.ru",
        to_addrs=to or [],
        cc_addrs=cc or [],
        dedup_hash="h",
        sent_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


def test_observer_thread_no_flags():
    # Кирилл ↔ Яна, я только в копии: ничего не может быть моим обязательством.
    emails = [
        _email("inbound", to=[{"email": "yana@x.ru"}], cc=[{"email": "me@ya.ru"}]),
        _email("inbound", to=[{"email": "kirill@x.ru"}], cc=[{"email": "me@ya.ru"}]),
    ]
    assert _thread_flags(emails, ME) == (False, False)
    assert _allowed_types(False, False) == set()


def test_me_in_to_allows_reply_and_delegate_only():
    emails = [_email("inbound", to=[{"email": "Me@Ya.ru"}, {"email": "other@x.ru"}])]
    flags = _thread_flags(emails, ME)
    assert flags == (False, True)  # normalization: Me@Ya.ru matches me@ya.ru
    assert _allowed_types(*flags) == {ActionType.NEED_MY_REPLY, ActionType.TO_DELEGATE}


def test_outbound_allows_promise_waiting_delegated_only():
    emails = [_email("outbound", to=[{"email": "kirill@x.ru"}])]
    flags = _thread_flags(emails, ME)
    assert flags == (True, False)
    assert _allowed_types(*flags) == {
        ActionType.MY_COMMITMENT,
        ActionType.WAITING_FOR_REPLY,
        ActionType.DELEGATED_TASK,
    }


def test_both_flags_allow_all_five():
    emails = [
        _email("inbound", to=[{"email": "me@ya.ru"}]),
        _email("outbound", to=[{"email": "kirill@x.ru"}]),
    ]
    flags = _thread_flags(emails, ME)
    assert flags == (True, True)
    assert len(_allowed_types(*flags)) == 5


def test_cc_only_never_counts_as_recipient():
    emails = [_email("inbound", to=[{"email": "yana@x.ru"}], cc=[{"email": "me@ya.ru"}])]
    assert _thread_flags(emails, ME) == (False, False)


def test_mail_tool_schema_offers_only_mail_types():
    """The tool enum used to be built from every ActionType member, so a type produced by
    another source (the retired prep gate's MEETING_PREP, the meeting router's DECISION) was
    silently offered to the mail extractor."""
    from email_reader.ai.schemas import MAIL_ACTION_TYPES, TOOL_SCHEMA

    offered = set(TOOL_SCHEMA["input_schema"]["properties"]["items"]["items"]
                  ["properties"]["type"]["enum"])
    assert ActionType.MEETING_PREP not in offered
    assert ActionType.DECISION not in offered
    # TASK (a task to self) is actionable but comes only from meetings, quick-add and the
    # bot — in mail there is always someone on the other end (docs/ux-tasks.md §7.1).
    assert ActionType.TASK not in offered
    assert offered == {t.value for t in MAIL_ACTION_TYPES}


def test_decision_is_not_actionable():
    # A decision has no lifecycle — it can't be "closed", so it must stay off the boards.
    from email_reader.enums import ACTIONABLE_TYPES

    assert ActionType.DECISION not in ACTIONABLE_TYPES
