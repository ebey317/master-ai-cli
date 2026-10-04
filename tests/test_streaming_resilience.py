"""Tests for the streaming resilience helpers (portable design invariants)."""
import pytest
from scripts.streaming_resilience import (
    StreamDeliveryTracker,
    StreamErrorClassifier,
    is_transient_error,
    make_partial_stream_stub,
)


class TestStreamDeliveryTracker:
    def test_deltas_vs_visible_text(self):
        t = StreamDeliveryTracker()
        assert not t.deltas_were_sent
        assert not t.visible_text_delivered()

        t.mark_delta_sent()  # whitespace/think-only delta
        assert t.deltas_were_sent
        assert not t.visible_text_delivered()  # still no visible text

        t.mark_visible_text("  ")  # only whitespace
        assert not t.visible_text_delivered()

        t.mark_visible_text("Hello")  # first visible char
        assert t.visible_text_delivered()

    def test_tool_call_in_flight(self):
        t = StreamDeliveryTracker()
        assert not t.partial_tool_in_flight()

        t.mark_tool_call_start("read_file")
        assert t.partial_tool_in_flight()
        assert "read_file" in t.partial_tool_names

        t.mark_tool_call_end()
        # provider_tool_in_flight cleared, but partial_tool_names retained
        assert not t.provider_tool_in_flight
        assert t.partial_tool_in_flight()  # still true via partial_tool_names

    def test_reset_for_retry_clears_transient_state_only(self):
        t = StreamDeliveryTracker()
        t.mark_delta_sent()
        t.mark_visible_text("Hello")
        t.mark_tool_call_start("write_file")

        t.reset_for_retry()

        assert not t.deltas_were_sent
        assert not t.first_delta_fired
        assert t.visible_text_buffer == ""
        # Tool call state preserved (provider-side)
        assert t.partial_tool_in_flight()
        assert "write_file" in t.partial_tool_names


class TestStreamErrorClassifier:
    def test_undelivered_failure_retries(self):
        """Deltas fired, no visible text, no tool → retry as undelivered failure."""
        tracker = StreamDeliveryTracker()
        tracker.mark_delta_sent()  # think-only delta
        # No mark_visible_text called

        classifier = StreamErrorClassifier(max_retries=3, is_transient_fn=is_transient_error)
        decision = classifier.classify(Exception("connection reset"), attempt=1, tracker=tracker)

        assert decision.should_retry
        assert "undelivered" in decision.reason

    def test_undelivered_failure_exhausted_raises(self):
        """Same scenario but budget exhausted → propagate error."""
        tracker = StreamDeliveryTracker()
        tracker.mark_delta_sent()

        classifier = StreamErrorClassifier(max_retries=1, is_transient_fn=is_transient_error)
        decision = classifier.classify(Exception("connection reset"), attempt=1, tracker=tracker)

        assert not decision.should_retry
        assert decision.error is not None
        assert "budget exhausted" in decision.reason

    def test_partial_delivery_with_visible_text_no_retry(self):
        """Visible text delivered → real partial delivery, no retry."""
        tracker = StreamDeliveryTracker()
        tracker.mark_delta_sent()
        tracker.mark_visible_text("Hello world")

        classifier = StreamErrorClassifier(max_retries=3, is_transient_fn=is_transient_error)
        decision = classifier.classify(Exception("timeout"), attempt=1, tracker=tracker)

        assert not decision.should_retry
        assert decision.error is not None
        assert "partial delivery" in decision.reason

    def test_partial_delivery_with_tool_call_retries_transient(self):
        """Tool call in flight + transient error → retry (reconnect beats failed action)."""
        tracker = StreamDeliveryTracker()
        tracker.mark_delta_sent()
        tracker.mark_tool_call_start("bash")

        classifier = StreamErrorClassifier(max_retries=3, is_transient_fn=is_transient_error)
        decision = classifier.classify(Exception("timeout"), attempt=1, tracker=tracker)

        assert decision.should_retry
        assert "tool call in flight" in decision.reason

    def test_partial_delivery_with_tool_call_no_retry_non_transient(self):
        """Tool call in flight + non-transient error → no retry."""
        tracker = StreamDeliveryTracker()
        tracker.mark_delta_sent()
        tracker.mark_tool_call_start("bash")

        classifier = StreamErrorClassifier(max_retries=3, is_transient_fn=lambda e: False)
        decision = classifier.classify(Exception("bad request"), attempt=1, tracker=tracker)

        assert not decision.should_retry
        assert "non-transient" in decision.reason

    def test_no_deltas_standard_transient_logic(self):
        """No deltas sent → standard transient retry logic applies."""
        tracker = StreamDeliveryTracker()  # deltas_were_sent = False

        classifier = StreamErrorClassifier(max_retries=3, is_transient_fn=is_transient_error)
        decision = classifier.classify(Exception("timeout"), attempt=1, tracker=tracker)

        assert decision.should_retry
        assert "before any delta" in decision.reason


class TestPartialStreamStub:
    def test_stub_contains_visible_text_only(self):
        tracker = StreamDeliveryTracker()
        tracker.mark_visible_text("  \n  ")  # whitespace only
        tracker.mark_visible_text("Actual content")

        stub = make_partial_stream_stub(tracker, Exception("timeout"))

        assert stub["content"] == "Actual content"
        assert stub["finish_reason"] == "length"
        assert stub["tool_calls"] is None

    def test_stub_empty_content_when_only_whitespace(self):
        tracker = StreamDeliveryTracker()
        tracker.mark_visible_text("   \n\t  ")

        stub = make_partial_stream_stub(tracker, Exception("timeout"))

        # Empty string becomes None per stub contract
        assert stub["content"] is None

    def test_stub_preserves_partial_tool_names(self):
        tracker = StreamDeliveryTracker()
        tracker.mark_tool_call_start("read_file")
        tracker.mark_tool_call_start("write_file")

        stub = make_partial_stream_stub(tracker, Exception("timeout"))

        assert stub["partial_tool_names"] == ["read_file", "write_file"]
        assert stub["tool_calls"] is None  # blocked


class TestIsTransientError:
    def test_network_errors_transient(self):
        import httpx
        assert is_transient_error(httpx.TimeoutException("timeout"))
        assert is_transient_error(httpx.NetworkError("dns"))
        assert is_transient_error(httpx.RemoteProtocolError("protocol"))

    def test_5xx_transient(self):
        import httpx
        resp = httpx.Response(503, request=httpx.Request("GET", "http://x"))
        assert is_transient_error(httpx.HTTPStatusError("503", request=resp.request, response=resp))

    def test_4xx_non_transient(self):
        import httpx
        resp = httpx.Response(400, request=httpx.Request("GET", "http://x"))
        assert not is_transient_error(httpx.HTTPStatusError("400", request=resp.request, response=resp))
        resp = httpx.Response(401, request=httpx.Request("GET", "http://x"))
        assert not is_transient_error(httpx.HTTPStatusError("401", request=resp.request, response=resp))
