"""Tests for the batch-aware clarify helper."""

import json
from scripts.clarify_batch import ask_clarify_batch, make_clarify_callback


def test_batch_stops_at_first_unanswered():
    """Batch should stop at first unanswered question and set timed_out=True."""
    calls = []

    def ask_one(q, choices, multi):
        calls.append(q)
        if q == "Two?":
            return "", False  # unanswered
        return "yes", True

    questions = [
        {"question": "One?", "qid": "q1"},
        {"question": "Two?", "qid": "q2"},
        {"question": "Three?", "qid": "q3"},
    ]
    result = ask_clarify_batch(questions, ask_one)
    payload = json.loads(result)

    assert payload == {"answers": {"q1": "yes"}, "timed_out": True}
    assert calls == ["One?", "Two?"]  # stops after first unanswered


def test_batch_all_answered_no_timeout():
    """All answered -> timed_out=False, all answers collected."""
    def ask_one(q, choices, multi):
        return f"answer to {q}", True

    questions = [
        {"question": "A?", "qid": "a"},
        {"question": "B?", "qid": "b"},
    ]
    result = ask_clarify_batch(questions, ask_one)
    payload = json.loads(result)

    assert payload == {"answers": {"a": "answer to A?", "b": "answer to B?"}, "timed_out": False}


def test_callback_single_form_returns_raw_string():
    """Legacy single-question form returns raw response string (not JSON)."""
    def ask_one(q, choices, multi):
        return "single answer", True

    cb = make_clarify_callback(ask_one)
    result = cb(question="Single?", choices=None, multi_select=False)

    assert result == "single answer"  # raw string, not JSON


def test_callback_batch_form_returns_json():
    """Batch form returns structured JSON with answers and timed_out."""
    def ask_one(q, choices, multi):
        return "batch answer", True

    cb = make_clarify_callback(ask_one)
    result = cb(questions=[{"question": "Q1?"}, {"question": "Q2?"}])
    payload = json.loads(result)

    assert payload == {"answers": {"q0": "batch answer", "q1": "batch answer"}, "timed_out": False}


def test_batch_uses_qid_when_provided():
    """Custom qid fields are respected in answers dict."""
    def ask_one(q, choices, multi):
        return "ok", True

    questions = [
        {"question": "First?", "qid": "custom-id"},
        {"question": "Second?"},  # auto qid
    ]
    result = ask_clarify_batch(questions, ask_one)
    payload = json.loads(result)

    assert "custom-id" in payload["answers"]
    assert "q1" in payload["answers"]  # auto-generated for second
