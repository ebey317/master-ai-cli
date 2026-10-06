"""
Streaming resilience helpers for Sensei.

Portable reimplementation of the "undelivered stream failure" classification:
- Tracks whether *visible* (non-whitespace) assistant text reached a consumer
- Distinguishes "deltas fired" from "visible text delivered"
- On stream error: if deltas fired but no visible text delivered and no tool call
  in flight → treat as undelivered failure (retry); otherwise → partial delivery (no retry).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class StreamDeliveryTracker:
    """
    Tracks streaming delivery state for a single request attempt.

    Mirrors the upstream idea: ``deltas_were_sent`` flips on *any* content delta
    (including whitespace/think-only), while ``visible_text_delivered`` only
    becomes true when scrubbed, non-whitespace text actually reaches a consumer.
    """

    deltas_were_sent: bool = False
    first_delta_fired: bool = False
    visible_text_buffer: str = ""
    partial_tool_names: list[str] = field(default_factory=list)
    provider_tool_in_flight: bool = False

    def mark_delta_sent(self) -> None:
        """Called when *any* content delta is emitted (including whitespace/reasoning)."""
        self.deltas_were_sent = True
        self.first_delta_fired = True

    def mark_visible_text(self, text: str) -> None:
        """Called when scrubbed text is about to be pushed to a consumer (TUI, log, subagent)."""
        if text and text.strip():
            self.visible_text_buffer += text

    def mark_tool_call_start(self, tool_name: str) -> None:
        self.partial_tool_names.append(tool_name)
        self.provider_tool_in_flight = True

    def mark_tool_call_end(self) -> None:
        self.provider_tool_in_flight = False

    def visible_text_delivered(self) -> bool:
        """True iff non-whitespace text actually reached a consumer this attempt."""
        return bool(self.visible_text_buffer.strip())

    def partial_tool_in_flight(self) -> bool:
        return bool(self.partial_tool_names) or self.provider_tool_in_flight

    def reset_for_retry(self) -> None:
        """Reset transient state for a same-prefix retry after an undelivered failure."""
        self.deltas_were_sent = False
        self.first_delta_fired = False
        self.visible_text_buffer = ""
        # Keep partial_tool_names/provider_tool_in_flight as they reflect provider-side state


class StreamErrorClassifier:
    """
    Classifies streaming errors and decides retry/fallback behavior.

    Usage:
        tracker = StreamDeliveryTracker()
        classifier = StreamErrorClassifier(max_retries=3, is_transient_fn=my_transient_check)

        try:
            async for chunk in stream():
                tracker.mark_delta_sent()
                if is_visible(chunk):
                    tracker.mark_visible_text(chunk.text)
                yield chunk
        except Exception as e:
            action = classifier.classify(e, attempt=1, tracker=tracker)
            if action.should_retry:
                tracker.reset_for_retry()
                # retry the same request...
            else:
                # propagate to conversation loop fallback/backoff
                raise action.error or e
    """

    def __init__(
        self,
        max_retries: int,
        is_transient_fn: Callable[[Exception], bool],
        on_undelivered_retry: Callable[[Exception], None] | None = None,
    ):
        self.max_retries = max_retries
        self.is_transient_fn = is_transient_fn
        self.on_undelivered_retry = on_undelivered_retry or (lambda e: None)

    @dataclass
    class Decision:
        should_retry: bool
        error: Exception | None = None
        reason: str = ""

    def classify(
        self,
        error: Exception,
        attempt: int,
        tracker: StreamDeliveryTracker,
    ) -> Decision:
        """
        Classify a stream error and decide whether to retry.

        Returns a Decision with should_retry=True for:
        - Transient errors with budget remaining (standard path)
        - Undelivered stream failures (deltas fired but no visible text, no tool in flight)
          with budget remaining
        Returns should_retry=False for:
        - Non-transient errors
        - Budget exhausted
        - Real partial delivery (visible text delivered OR tool call in flight)
        """
        # Provider-level retry signals (e.g. stream_options rejected) handled upstream
        # Here we focus on the "partial vs undelivered" classification.

        if not tracker.deltas_were_sent:
            # Nothing even left the provider — standard transient retry logic applies
            if self.is_transient_fn(error) and attempt < self.max_retries:
                return self.Decision(True, reason="transient error before any delta")
            return self.Decision(
                False, error, "non-transient or budget exhausted before any delta"
            )

        # Deltas *did* fire. Now check if anything visible reached a consumer.
        if tracker.partial_tool_in_flight():
            # Tool call in flight — aborting discards it; retry transient errors
            # (reconnecting + duplicated preamble beats a failed action; no tool executed yet)
            if self.is_transient_fn(error) and attempt < self.max_retries:
                return self.Decision(
                    True, reason="transient error with tool call in flight"
                )
            return self.Decision(
                False, error, "non-transient or budget exhausted with tool in flight"
            )

        if not tracker.visible_text_delivered():
            # Deltas fired (whitespace/think-only or no consumer) but NOTHING visible delivered.
            # From user/model perspective: NOTHING was delivered. Empty stub would cause
            # "continue from nowhere" nudge → model repeats lost step (#112419).
            # Treat as undelivered failure: same-prefix retry, then loop fallback/backoff.
            logger.warning(
                "Stream died after deltas but before any visible text delivered "
                "(0 visible chars, no tool call in flight); treating as undelivered stream failure: %s",
                error,
            )
            if attempt < self.max_retries:
                self.on_undelivered_retry(error)
                return self.Decision(True, reason="undelivered stream failure (retry)")
            return self.Decision(
                False, error, "undelivered stream failure (budget exhausted)"
            )

        # Visible text *was* delivered → real partial delivery.
        # No retry (would duplicate text). Error propagates to conversation loop.
        logger.warning(
            "Streaming failed after partial delivery (visible text delivered), not retrying: %s",
            error,
        )
        return self.Decision(False, error, "partial delivery with visible text")


def is_transient_error(error: Exception) -> bool:
    """
    Default transient error checker — override with provider-specific logic.

    Treat as transient: network errors, timeouts, 5xx, stream corruption.
    Treat as non-transient: 4xx (bad request), auth errors, context length.
    Also recognizes plain-Exception messages naming timeouts — provider
    SDKs and wrappers raise bare Exception(\"timeout\")/Exception(\"timed out\")
    after swallowing the typed error, and a timeout is still a timeout.
    """
    import httpx

    if isinstance(
        error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
    ):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return 500 <= error.response.status_code < 600
    msg = str(error).lower()
    return (
        "timeout" in msg
        or "timed out" in msg
        or "temporarily unavailable" in msg
        or "connection reset" in msg
    )


def make_partial_stream_stub(
    tracker: StreamDeliveryTracker,
    error: Exception,
    agent_id: str = "assistant",
) -> dict:
    """
    Build a finish_reason="length" stub for *real* partial delivery.

    Content may be EMPTY (dropped tool call, overflow) — the loop skips appending
    an empty stub and only sends the nudge. A text-only death with 0 visible
    chars never gets here: the error handler reclassifies it as undelivered.
    """
    partial_text = tracker.visible_text_buffer.strip() or None
    partial_names = list(tracker.partial_tool_names)

    return {
        "id": f"partial-stream-{agent_id}",
        "role": "assistant",
        "content": partial_text,
        "tool_calls": None,  # block executing incomplete calls
        "finish_reason": "length",
        "error": error,
        "partial_tool_names": partial_names,
    }
