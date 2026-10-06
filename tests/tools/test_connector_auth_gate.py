"""Tests for connector availability gate."""

from scripts.tools.connector_auth_gate import AuthState, connectors_available


def test_available_when_config_and_auth():
    def check() -> AuthState:
        return AuthState(connectors_enabled=True, user_authenticated=True)

    assert connectors_available(check) is True


def test_unavailable_when_config_off():
    def check() -> AuthState:
        return AuthState(connectors_enabled=False, user_authenticated=True)

    assert connectors_available(check) is False


def test_unavailable_when_not_signed_in():
    def check() -> AuthState:
        return AuthState(connectors_enabled=True, user_authenticated=False)

    assert connectors_available(check) is False


def test_fail_closed_on_exception():
    def check() -> AuthState:
        raise RuntimeError("auth service down")

    assert connectors_available(check) is False
