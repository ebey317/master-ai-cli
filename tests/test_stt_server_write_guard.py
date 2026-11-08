"""Test the Pupil write guard without binding a socket or touching real keys."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "stt_guard_test", Path(__file__).resolve().parents[1] / "stt_server.py"
)
assert spec is not None and spec.loader is not None
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


@pytest.mark.parametrize(
    "host,sent,expected,allowed",
    [
        ("192.0.2.8", "", "token", False),
        ("192.0.2.8", "wrong", "token", False),
        ("192.0.2.8", "token", "", False),
        ("192.0.2.8", "token", "token", True),
        ("127.0.0.1", "", "token", True),
    ],
)
def test_write_guard(host, sent, expected, allowed):
    results = []
    handler = SimpleNamespace(
        client_address=(host, 1000),
        headers={"X-Master-AI-Token": sent},
        _extension_token=lambda: expected,
        _json=lambda body, status: results.append((body, status)),
    )
    assert server.Handler._require_local_or_token(handler) is allowed
    assert results == ([] if allowed else [({"error": "local or token required"}, 403)])


@pytest.mark.parametrize("route", ["/keys", "/sessions/delete"])
@pytest.mark.parametrize(
    "host,sent,allowed",
    [
        ("192.0.2.8", "", False),
        ("192.0.2.8", "wrong", False),
        ("192.0.2.8", "token", True),
        ("127.0.0.1", "", True),
    ],
)
def test_actual_post_write_routes_are_guarded(
    tmp_path, monkeypatch, route, host, sent, allowed
):
    keyfile = tmp_path / "keys.json"
    keyfile.write_text("{}")
    chat = tmp_path / "chat.json"
    chat.write_text("chat")
    expand = server.os.path.expanduser
    monkeypatch.setattr(
        server.os.path,
        "expanduser",
        lambda p: str(keyfile) if p == "~/.master_ai_keys" else expand(p),
    )
    monkeypatch.setattr(server, "_chats_dir", lambda: str(tmp_path))
    payload = (
        {"field": "unit", "value": "data"}
        if route == "/keys"
        else {"file": "chat.json"}
    )
    body = json.dumps(payload).encode()
    handler = server.Handler.__new__(server.Handler)
    handler.path = route
    handler.client_address = (host, 123)
    handler.headers = {"Content-Length": str(len(body)), "X-Master-AI-Token": sent}
    handler.rfile = io.BytesIO(body)
    handler._extension_token = lambda: "token"
    responses = []
    handler._json = lambda data, status=200: responses.append((data, status))
    handler.do_POST()
    assert responses[-1][1] == (200 if allowed else 403)
    if route == "/keys":
        assert json.loads(keyfile.read_text()) == ({"unit": "data"} if allowed else {})
    else:
        assert chat.exists() is not allowed
