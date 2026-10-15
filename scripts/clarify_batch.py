"""
Batch-aware clarify helper for Sensei tools.

Portable pattern from upstream: a tool callback that can receive either a single
question or a `questions=[...]` batch should answer the batch at the surface,
returning a structured JSON payload with explicit `timed_out` flag instead of
sentinel strings. The per-question primitive returns (response, answered) so
the caller decides what "no answer" means.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

# Type for the primitive that asks one question and returns (response, answered)
AskOneFn = Callable[[str, list[str] | None, bool], tuple[str, bool]]


def ask_clarify_batch(
    questions: list[dict[str, Any]],
    ask_one: AskOneFn,
    *,
    rearm_on_last: bool = True,
) -> str:
    """
    Answer a clarify batch: one card per question, stop at the first unanswered.

    Args:
        questions: List of question dicts with keys: "question", "choices",
            "multi_select", "qid" (optional).
        ask_one: Callable(question, choices, multi_select) -> (response, answered).
        rearm_on_last: If True, re-arm typing/input on the last question only.

    Returns:
        JSON string: {"answers": {qid: response}, "timed_out": bool}
    """
    answers: dict[str, str] = {}
    timed_out = False

    for index, entry in enumerate(questions):
        qtext = entry.get("question", "")
        choices = entry.get("choices")
        multi_select = bool(entry.get("multi_select", False))
        qid = entry.get("qid") or f"q{index}"
        is_last = index == len(questions) - 1

        # The primitive returns (raw_response, answered_boolean)
        response, answered = ask_one(qtext, choices, multi_select)

        if not answered:
            timed_out = True
            break

        answers[qid] = response

    return json.dumps({"answers": answers, "timed_out": timed_out}, ensure_ascii=False)


def make_clarify_callback(
    ask_one: AskOneFn,
) -> Callable[..., str]:
    """
    Factory that wraps a single-question primitive into a callback compatible
    with both the legacy single-question signature and the batch `questions=`
    form.

    The returned callable accepts either:
      - question: str, choices, multi_select  (legacy single form)
      - questions: List[Dict]                 (batch form)

    and always returns a JSON string for the batch form, or the raw response
    string for the single form (preserving backward compatibility).
    """

    def callback(
        question: str | None = None,
        choices: list[str] | None = None,
        multi_select: bool = False,
        questions: list[dict[str, Any]] | None = None,
    ) -> str:
        if questions is not None:
            return ask_clarify_batch(questions, ask_one)
        # Single-question path: preserve exact legacy behavior (raw string)
        response, _answered = ask_one(question or "", choices, multi_select)
        return response

    return callback


# Example primitive for a TUI/terminal surface — replace with real implementation
def example_ask_one(
    question: str, choices: list[str] | None, multi_select: bool
) -> tuple[str, bool]:
    """
    Example primitive: presents one question, blocks for answer.
    Returns (response_text, answered_bool).
    """
    # In real code: render card, wait for user input with timeout, retire card.
    # answered=False means timeout or delivery failure.
    # This stub always "answers" for demonstration.
    return "user response", True


# Default callback ready for tool registration
default_clarify_callback = make_clarify_callback(example_ask_one)
