from __future__ import annotations

from email_reader.eval.scorer import TypeScore, _actionable, _key


def test_typescore_precision_recall_f1():
    s = TypeScore(tp=8, fp=2, fn=2)
    assert abs(s.precision - 0.8) < 1e-9
    assert abs(s.recall - 0.8) < 1e-9
    assert abs(s.f1 - 0.8) < 1e-9


def test_typescore_empty_is_perfect():
    s = TypeScore()
    assert s.precision == 1.0 and s.recall == 1.0


def test_key_normalizes_counterparty():
    assert _key("WAITING_FOR_REPLY", "  Ivan ") == ("WAITING_FOR_REPLY", "ivan")
    assert _key("INFO", None) == ("INFO", "")


def test_actionable_drops_info():
    items = [
        {"type": "WAITING_FOR_REPLY", "counterparty": "Ivan"},
        {"type": "INFO", "counterparty": None},
        {"type": "MY_COMMITMENT", "counterparty": "Anna"},
    ]
    keys = _actionable(items)
    assert ("WAITING_FOR_REPLY", "ivan") in keys
    assert ("MY_COMMITMENT", "anna") in keys
    assert all(k[0] != "INFO" for k in keys)
