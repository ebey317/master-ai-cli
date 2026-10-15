import time

from scripts.limit_reset_formatter import (
    build_hook_payload,
    extract_resets_at,
    format_limit_reset,
)


def test_resets_at_direct():
    assert extract_resets_at({"resets_at": 1700000000}) == 1700000000.0


def test_retry_after_seconds():
    resets_at = extract_resets_at({"retry_after": 600})
    assert 590 < resets_at - time.time() < 610


def test_retry_after_header_string():
    resets_at = extract_resets_at({"headers": {"Retry-After": "30"}})
    assert resets_at > time.time()


def test_message_fallback():
    assert extract_resets_at("HTTP 429 ... resets_at=1700000000 abcd") == 1700000000.0


def test_format_line():
    now = 1_700_000_000
    line = format_limit_reset(now + 3600, now=now)
    assert line.startswith("Limit resets at ")
    assert "(in 1h 00m)" in line


def test_format_none_when_passed():
    assert format_limit_reset(time.time() - 5) is None


def test_format_none_when_unknown():
    assert format_limit_reset(None) is None


def test_hook_payload_shape():
    payload = build_hook_payload({"resets_at": 1700000000})
    assert payload["resets_at"] == 1700000000.0
    assert payload["informational_only"] is True
