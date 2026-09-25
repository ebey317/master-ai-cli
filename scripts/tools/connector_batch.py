"""
Interruptible connector batch execution for Sensei.

The upstream fix: /stop only checked the interrupt flag between *tools*, and
a connector batch was treated as one tool, so 20 remote calls would all fire
even after the user typed /stop. The portable idea is to check the interrupt
flag before every entry in a connector batch and return a clean INTERRUPTED
slot for entries that never started.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass
class BatchResult:
    name: str
    arguments: dict
    ok: bool
    code: str  # e.g. "OK", "INTERRUPTED", "ERROR"
    value: object


# Hook supplied by the caller (typically core dispatch). It must be a
# function that returns True when the user has requested a stop.
IsInterrupted = Callable[[], bool]


def run_batch(
    calls: Sequence[tuple[str, dict]],
    execute: Callable[[str, dict], object],
    is_interrupted: IsInterrupted,
) -> list[BatchResult]:
    """Execute connector calls sequentially, honoring /stop between entries.

    Args:
        calls: ordered (tool_name, arguments) pairs from the model.
        execute: function that performs one remote call and returns its value.
        is_interrupted: returns True if the session has been interrupted.

    Returns:
        One BatchResult per input call. Stopped entries carry code
        "INTERRUPTED" and a message value so the result envelope stays valid.
    """
    results: list[BatchResult] = []

    for name, arguments in calls:
        if is_interrupted():
            results.append(
                BatchResult(
                    name=name,
                    arguments=arguments,
                    ok=False,
                    code="INTERRUPTED",
                    value="Stopped by the user before this call was made.",
                )
            )
            continue

        try:
            value = execute(name, arguments)
            results.append(
                BatchResult(
                    name=name,
                    arguments=arguments,
                    ok=True,
                    code="OK",
                    value=value,
                )
            )
        except Exception as exc:
            results.append(
                BatchResult(
                    name=name,
                    arguments=arguments,
                    ok=False,
                    code="ERROR",
                    value=f"{type(exc).__name__}: {exc}",
                )
            )

    return results
