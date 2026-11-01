from __future__ import annotations

from datetime import UTC, datetime

from email_reader.ai import prompt as P


def _msg(body, is_me=False):
    return {"is_me": is_me, "who": "Bob", "sent_at": datetime(2026, 6, 1, tzinfo=UTC),
            "subject": "S", "body": body}


def test_render_thread_caps_giant_body():
    """A pathological thread must not blow past the model context window."""
    huge = "x" * 2_000_000  # ~2MB single message
    out = P.render_thread([_msg(huge)], datetime(2026, 6, 1, tzinfo=UTC).date())
    assert len(out) <= P._MAX_THREAD_CHARS + 64
    assert "обрезан" in out


def test_render_thread_caps_many_messages():
    msgs = [_msg("body " * 500) for _ in range(2000)]  # many medium messages
    out = P.render_thread(msgs, datetime(2026, 6, 1, tzinfo=UTC).date())
    assert len(out) <= P._MAX_THREAD_CHARS + 64


def test_render_thread_keeps_small_threads_intact():
    out = P.render_thread([_msg("привет, подтверди до пятницы")],
                          datetime(2026, 6, 1, tzinfo=UTC).date())
    assert "привет, подтверди до пятницы" in out
    assert "обрезан" not in out
