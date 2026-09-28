"""Regression tests for the master_ai.py parsing-baseline rescue (c90ff0a).

Two defects fixed alongside the rescue:

1. ask_model_router's local /api/chat auto-continue loop read `_finish`
   outside the try block that assigns it. A transport error on the first
   request broke out of the loop with `_finish` unbound, so the dispatch
   below raised UnboundLocalError -- violating the function's documented
   "Never raises; errors become None/empty text and are logged" contract.
   Exercised here by making urlopen raise, and asserting a (None, elapsed)
   tuple comes back instead of an exception.

2. _reply_needs_operator_input matched the ASK: directive with a bare
   substring test, so words merely ending in "ask" (task:, mask:, basket:)
   were read as an operator question and stalled auto-continuation.
   Exercised here by calling the public predicate on real reply text.

These assert observable return values through the real functions; they do
not read or match source text.
"""

from __future__ import annotations

import math
import urllib.error

import pytest


@pytest.fixture
def ma(monkeypatch):
    """Import master_ai with its network and stateful side effects stubbed."""
    import master_ai

    monkeypatch.setattr(master_ai, "log", lambda *a, **k: None)
    monkeypatch.setattr(master_ai, "_record_real_ctx_tokens", lambda *a, **k: None)
    return master_ai


def _ok_response(content: str, finish_reason: str = "stop"):
    class _Resp:
        def read(self):
            import json

            return json.dumps(
                {
                    "message": {"content": content},
                    "finish_reason": finish_reason,
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _Resp()


# ── 1. ask_model_router must not raise when the local transport fails ──


def test_router_returns_none_when_local_transport_raises(ma, monkeypatch):
    """A connection error on the FIRST request must yield (None, elapsed).

    Before the fix this raised UnboundLocalError: `_finish` was assigned only
    inside the try block, and the `if _finish == "length"` dispatch read it
    after the loop had already broken on the exception.
    """
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ma.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(ma, "PINNED_MODEL", "qwen2.5vl:3b")
    monkeypatch.setattr(ma, "MAX_TOKENS", 512, raising=False)

    text, elapsed = ma.ask_model_router(
        [{"role": "user", "content": "hi"}], max_tokens=512
    )

    assert calls["n"] == 1, "should stop after the first failure, not retry"
    assert text is None, f"expected None on transport error, got {text!r}"
    assert isinstance(elapsed, (int, float)) and not math.isnan(elapsed)
    # The failed attempt must not leave stale continuation state behind.
    assert ma.PENDING_CONTINUATION is None


def test_router_error_path_does_not_leave_pending_continuation(ma, monkeypatch):
    """PENDING_CONTINUATION must be cleared, not left holding a stale entry.

    Set a stale value first, then force a failure: a caller that later reads
    PENDING_CONTINUATION must not see the previous turn's continuation.
    """
    ma.PENDING_CONTINUATION = {"provider": "local", "so_far": "stale text"}

    def _boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ma.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(ma, "PINNED_MODEL", "qwen2.5vl:3b")

    text, _ = ma.ask_model_router([{"role": "user", "content": "hi"}], max_tokens=512)

    assert text is None
    assert ma.PENDING_CONTINUATION is None, "stale continuation survived a failure"


def test_router_success_returns_text(ma, monkeypatch):
    """Sanity guard: the fixed loop still returns content on the happy path."""
    monkeypatch.setattr(
        ma.urllib.request,
        "urlopen",
        lambda *a, **k: _ok_response("hello from local"),
    )
    monkeypatch.setattr(ma, "PINNED_MODEL", "qwen2.5vl:3b")

    text, _ = ma.ask_model_router([{"role": "user", "content": "hi"}], max_tokens=512)

    assert text == "hello from local"
    assert ma.PENDING_CONTINUATION is None


# ── 2. _reply_needs_operator_input must not false-positive on "ask" words ──


@pytest.mark.parametrize(
    "reply",
    [
        "Marked task: build the parser",
        "Added mask: admin",
        "Picked basket: large",
        "She was asking about the router",
        "Ran the task: done",
    ],
)
def test_words_ending_in_ask_are_not_operator_questions(ma, reply):
    """Words that merely end in "ask" are announcements, not questions.

    The old `"ask:" in lowered` test matched the "ask:" inside "task:",
    "mask:", "basket:", which made the watchdog think the model was waiting
    on the operator and suppressed auto-continuation.
    """
    assert ma._reply_needs_operator_input(reply) is False


def test_real_ask_directive_is_still_detected(ma):
    """The fix must not break the signal it was protecting."""
    assert ma._reply_needs_operator_input("ASK: which one should I use?") is True
    assert ma._reply_needs_operator_input("ask : confirm the plan") is True


def test_genuine_questions_still_detected(ma):
    """A trailing question mark and the phrase list keep working."""
    assert ma._reply_needs_operator_input("Want me to continue?") is True
    assert ma._reply_needs_operator_input("Let me know which you prefer") is True


def test_plain_announcement_is_not_a_question(ma):
    """The negative case still holds for text with no ask signal at all."""
    assert ma._reply_needs_operator_input("Rebuilt the index.") is False
    assert ma._reply_needs_operator_input("") is False
