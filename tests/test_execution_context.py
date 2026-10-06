"""Tests for execution context detection and approval gating.

Ported from NousResearch/hermes-agent tests:
- test_cron_not_interactive.py
- test_cronjob_tools.py (external worker scenario)
"""

import pytest

from scripts.execution_context import (
    detect_context_kind,
    presence_flags,
    check_cronjob_requirements,
    is_truly_unattended,
    has_async_approval_bridge,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove all SENSEI_* vars before each test."""
    for key in list(os.environ.keys()):
        if key.startswith("SENSEI_"):
            monkeypatch.delenv(key, raising=False)


import os  # noqa: E402


def test_interactive_session_keeps_presence(monkeypatch):
    monkeypatch.setenv("SENSEI_INTERACTIVE", "1")
    monkeypatch.setenv("SENSEI_GATEWAY_SESSION", "1")
    monkeypatch.setenv("SENSEI_EXEC_ASK", "1")

    kind = detect_context_kind()
    assert kind == "interactive"

    is_cli, is_gateway, is_ask = presence_flags()
    assert (is_cli, is_gateway, is_ask) == (True, True, True)


def test_single_query_clears_presence(monkeypatch):
    # Simulate leaked presence from parent process
    monkeypatch.setenv("SENSEI_INTERACTIVE", "1")
    monkeypatch.setenv("SENSEI_GATEWAY_SESSION", "1")
    monkeypatch.setenv("SENSEI_EXEC_ASK", "1")
    monkeypatch.setenv("SENSEI_SINGLE_QUERY", "1")

    kind = detect_context_kind()
    assert kind == "single_query"

    is_cli, is_gateway, is_ask = presence_flags()
    assert (is_cli, is_gateway, is_ask) == (False, False, False)
    assert is_truly_unattended() is True
    assert has_async_approval_bridge() is False


def test_cron_context_clears_presence(monkeypatch):
    monkeypatch.setenv("SENSEI_INTERACTIVE", "1")
    monkeypatch.setenv("SENSEI_GATEWAY_SESSION", "1")
    monkeypatch.setenv("SENSEI_EXEC_ASK", "1")
    monkeypatch.setenv("SENSEI_CRON_SESSION", "1")

    kind = detect_context_kind()
    assert kind == "cron"

    is_cli, is_gateway, is_ask = presence_flags()
    assert (is_cli, is_gateway, is_ask) == (False, False, False)
    assert is_truly_unattended() is True
    assert has_async_approval_bridge() is False


def test_api_server_platform_keeps_exec_ask_for_approval_bridge(monkeypatch):
    """api_server resolves approvals via approval.request -> POST /v1/runs/{id}/approval;
    clearing is_ask there would turn every dangerous command into an instant BLOCK."""
    monkeypatch.setenv("SENSEI_INTERACTIVE", "1")
    monkeypatch.setenv("SENSEI_GATEWAY_SESSION", "1")
    monkeypatch.setenv("SENSEI_EXEC_ASK", "1")
    monkeypatch.setenv("SENSEI_SESSION_PLATFORM", "api_server")

    kind = detect_context_kind()
    assert kind == "api_server"

    is_cli, is_gateway, is_ask = presence_flags()
    assert (is_cli, is_gateway, is_ask) == (False, True, True)
    assert is_truly_unattended() is False
    assert has_async_approval_bridge() is True


def test_webhook_platform_clears_ask_keeps_gateway(monkeypatch):
    monkeypatch.setenv("SENSEI_SESSION_PLATFORM", "webhook")

    kind = detect_context_kind()
    assert kind == "webhook"

    is_cli, is_gateway, is_ask = presence_flags()
    assert (is_cli, is_gateway, is_ask) == (False, True, False)


def test_cronjob_requirements_interactive_mode(monkeypatch):
    monkeypatch.setenv("SENSEI_INTERACTIVE", "1")
    assert check_cronjob_requirements() is True


def test_cronjob_requirements_gateway_session(monkeypatch):
    monkeypatch.setenv("SENSEI_GATEWAY_SESSION", "1")
    assert check_cronjob_requirements() is True


def test_cronjob_requirements_exec_ask(monkeypatch):
    monkeypatch.setenv("SENSEI_EXEC_ASK", "1")
    assert check_cronjob_requirements() is True


def test_cronjob_requirements_external_cron_worker_with_presence_vars_stripped(monkeypatch):
    """External cron worker has presence trio stripped from env;
    the cron session marker alone must keep cron.allow_agent_scheduling effective."""
    for var in ("SENSEI_INTERACTIVE", "SENSEI_GATEWAY_SESSION", "SENSEI_EXEC_ASK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SENSEI_CRON_SESSION", "1")

    assert check_cronjob_requirements() is True


@pytest.mark.parametrize("var_name", ["SENSEI_INTERACTIVE", "SENSEI_GATEWAY_SESSION", "SENSEI_EXEC_ASK"])
def test_cronjob_requirements_each_flag_sufficient(monkeypatch, var_name):
    for var in ("SENSEI_INTERACTIVE", "SENSEI_GATEWAY_SESSION", "SENSEI_EXEC_ASK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(var_name, "1")
    assert check_cronjob_requirements() is True
