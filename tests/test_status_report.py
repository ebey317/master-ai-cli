"""
Contract tests for scripts.status_report.

Ensures the builder produces consistent, display-ready fields and that
the status_lines renderer includes every common value.
"""

import datetime as dt
import pytest

from scripts.status_report import build_status_fields, status_lines, StatusFields


class DummyAgent:
    def __init__(
        self,
        model: str = "test-model",
        provider: str = "test-provider",
        session_total_tokens: int = 12345,
    ):
        self.model = model
        self.provider = provider
        self.session_total_tokens = session_total_tokens


def test_build_status_fields_minimal():
    """Minimal session with only required fields."""
    fields = build_status_fields("sess-123")
    assert fields.session_id == "sess-123"
    assert fields.title == ""
    assert fields.model == "(unknown)"
    assert fields.provider == "unknown"
    assert fields.tokens == "0"
    assert fields.agent_running is False
    assert fields.created_dt is not None
    assert fields.last_activity_dt is not None


def test_build_status_fields_with_agent_and_meta():
    """Full session with agent, metadata, and explicit overrides."""
    now = dt.datetime.now(dt.timezone.utc)
    agent = DummyAgent(model="gpt-4", provider="openai", session_total_tokens=50000)
    meta = {
        "title": "My Session",
        "started_at": now - dt.timedelta(hours=2),
        "updated_at": now - dt.timedelta(minutes=30),
    }
    fields = build_status_fields(
        "sess-456",
        agent=agent,
        session_meta=meta,
        agent_running=True,
    )
    assert fields.session_id == "sess-456"
    assert fields.title == "My Session"
    assert fields.model == "gpt-4"
    assert fields.provider == "openai"
    assert fields.tokens == "50,000"
    assert fields.agent_running is True
    assert fields.created_dt is not None
    assert fields.last_activity_dt is not None
    # created should be started_at
    assert abs((fields.created_dt - meta["started_at"]).total_seconds()) < 1
    # last_activity should be updated_at (higher priority than started_at)
    assert abs((fields.last_activity_dt - meta["updated_at"]).total_seconds()) < 1


def test_build_status_fields_fallback_chain():
    """Verify fallback priority: agent > explicit args > session_meta > defaults."""
    agent = DummyAgent(model="agent-model", provider="agent-provider")
    meta = {"model": "meta-model", "provider": "meta-provider"}

    # Agent wins
    fields = build_status_fields("sess-1", agent=agent, session_meta=meta)
    assert fields.model == "agent-model"
    assert fields.provider == "agent-provider"

    # Explicit args win over meta when no agent
    fields = build_status_fields(
        "sess-2",
        session_meta=meta,
        model="explicit-model",
        provider="explicit-provider",
    )
    assert fields.model == "explicit-model"
    assert fields.provider == "explicit-provider"

    # Meta wins when no agent, no explicit
    fields = build_status_fields("sess-3", session_meta=meta)
    assert fields.model == "meta-model"
    assert fields.provider == "meta-provider"


def test_build_status_fields_unparseable_timestamps_fallback_to_now():
    """Unparseable started_at falls back to created_fallback or now()."""
    meta = {"started_at": "not-a-timestamp", "updated_at": "also-bad"}
    fallback = dt.datetime(2024, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    fields = build_status_fields("sess-bad", session_meta=meta, created_fallback=fallback)
    assert fields.created_dt == fallback
    assert fields.last_activity_dt == fallback


def test_build_status_fields_tokens_from_session_meta_when_agent_zero():
    """Tokens fall back to session_meta when agent has zero."""
    agent = DummyAgent(session_total_tokens=0)
    meta = {"tokens": 999}
    fields = build_status_fields("sess-tok", agent=agent, session_meta=meta)
    assert fields.tokens_raw == 999
    assert fields.tokens == "999"


def test_status_lines_renders_all_common_fields():
    """status_lines() includes every common field in label:value form."""
    now = dt.datetime.now(dt.timezone.utc)
    fields = StatusFields(
        session_id="sess-789",
        title="Test Title",
        model="test-model",
        provider="test-provider",
        created="2024-01-01 12:00",
        last_activity="2024-01-01 12:30",
        tokens="1,234",
        agent_running=True,
        created_dt=now,
        last_activity_dt=now,
        tokens_raw=1234,
    )
    lines = status_lines(fields)
    joined = "\n".join(lines)
    assert "Session ID: sess-789" in joined
    assert "Title: Test Title" in joined
    assert "Model: test-model" in joined
    assert "Provider: test-provider" in joined
    assert "Created: 2024-01-01 12:00" in joined
    assert "Last Activity: 2024-01-01 12:30" in joined
    assert "Tokens: 1,234" in joined
    assert "Agent Running: Yes" in joined


def test_status_lines_omits_empty_title():
    """Empty title is not rendered."""
    fields = build_status_fields("sess-empty-title")
    lines = status_lines(fields)
    assert not any("Title:" in line for line in lines)


def test_status_lines_prefix():
    """Prefix is applied to each line."""
    fields = build_status_fields("sess-prefix")
    lines = status_lines(fields, prefix="  ")
    assert all(line.startswith("  ") for line in lines)


# Sabotage tests: verify contract detects regressions
def test_sabotage_builder_dropping_tokens():
    """If builder drops tokens, test should fail."""
    fields = build_status_fields("sess-tok", session_meta={"tokens": 42})
    assert fields.tokens == "42"  # Would fail if builder stopped setting tokens


def test_sabotage_renderer_hand_formatting_model():
    """If renderer hand-formats model line instead of using fields, test fails."""
    fields = build_status_fields("sess-model", model="custom-model", provider="custom-provider")
    lines = status_lines(fields)
    # Ensure the line uses the structured field, not a hardcoded string
    model_lines = [l for l in lines if l.strip().startswith("Model:")]
    assert len(model_lines) == 1
    assert "custom-model" in model_lines[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
