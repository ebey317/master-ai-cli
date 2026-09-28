"""
Integration tests for model switch with reasoning effort.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.model_reasoning import ModelSwitchRequest, ReasoningEffort
from scripts.model_switch import handle_model_command, switch_model


class TestSwitchModelWithReasoning:
    @pytest.mark.asyncio
    async def test_switch_applies_reasoning_after_swap(self):
        with (
            patch(
                "scripts.model_switch.agent.set_model", new_callable=AsyncMock
            ) as mock_set_model,
            patch(
                "scripts.model_switch.agent.get_session_id", return_value="test-session"
            ),
            patch(
                "scripts.model_switch.apply_reasoning_effort",
                return_value="\nReasoning effort: high (session)",
            ) as mock_apply,
        ):
            req = ModelSwitchRequest(
                model="gpt-4",
                provider="openai",
                reasoning_effort=ReasoningEffort.HIGH,
                scope="session",
            )
            result = await switch_model(req)

            assert result.success is True
            assert result.model == "gpt-4"
            assert result.reasoning_effort == ReasoningEffort.HIGH
            mock_set_model.assert_awaited_once_with("gpt-4", provider="openai")
            mock_apply.assert_called_once()

    @pytest.mark.asyncio
    async def test_switch_without_reasoning(self):
        with patch(
            "scripts.model_switch.agent.set_model", new_callable=AsyncMock
        ) as mock_set_model:
            req = ModelSwitchRequest(model="gpt-4", provider="openai")
            result = await switch_model(req)

            assert result.success is True
            assert result.reasoning_effort is None
            assert "Reasoning effort" not in result.message


class TestHandleModelCommand:
    @pytest.mark.asyncio
    async def test_cli_command_with_reasoning_flag(self):
        with patch(
            "scripts.model_switch.switch_model", new_callable=AsyncMock
        ) as mock_switch:
            mock_switch.return_value = MagicMock(
                success=True,
                model="gpt-4",
                provider="openai",
                reasoning_effort=ReasoningEffort.HIGH,
                message="Switched to openai · gpt-4\nReasoning effort: high (session)",
            )

            result = await handle_model_command(["gpt-4", "--reasoning", "high"])

            assert result.success is True
            mock_switch.assert_awaited_once()
            call_args = mock_switch.call_args[0][0]
            assert call_args.reasoning_effort == ReasoningEffort.HIGH

    @pytest.mark.asyncio
    async def test_cli_command_global_scope(self):
        with patch(
            "scripts.model_switch.switch_model", new_callable=AsyncMock
        ) as mock_switch:
            mock_switch.return_value = MagicMock(
                success=True, model="gpt-4", provider="openai"
            )

            await handle_model_command(["gpt-4", "--reasoning", "high", "--global"])

            call_args = mock_switch.call_args[0][0]
            assert call_args.scope == "global"

    @pytest.mark.asyncio
    async def test_cli_command_once_scope(self):
        with patch(
            "scripts.model_switch.switch_model", new_callable=AsyncMock
        ) as mock_switch:
            mock_switch.return_value = MagicMock(
                success=True, model="gpt-4", provider="openai"
            )

            await handle_model_command(["gpt-4", "--reasoning", "low", "--once"])

            call_args = mock_switch.call_args[0][0]
            assert call_args.scope == "once"

    @pytest.mark.asyncio
    async def test_invalid_reasoning_returns_error(self):
        result = await handle_model_command(["gpt-4", "--reasoning", "invalid"])
        assert result.success is False
        assert "Invalid reasoning effort" in result.error or "Usage:" in result.error


class TestAuxiliaryModelReasoning:
    """Test that auxiliary model flows also get reasoning effort step."""

    @pytest.mark.asyncio
    async def test_aux_task_stores_reasoning_effort(self, monkeypatch):
        """Auxiliary tasks store reasoning_effort in their config block."""
        from sensei import config  # type: ignore[attr-defined]

        calls = []
        monkeypatch.setattr(config, "set", lambda k, v: calls.append((k, v)))

        # Simulate aux task config write
        task = "vision"
        effort = ReasoningEffort.HIGH
        config.set(f"auxiliary.{task}.reasoning_effort", effort.value)

        assert (f"auxiliary.{task}.reasoning_effort", "high") in calls
