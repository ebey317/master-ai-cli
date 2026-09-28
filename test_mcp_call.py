"""Tests for local MCP invocation — sensei_mcp_client.call_tool + MCP_CALL.

Until 2026-09-28 the client could DISCOVER a server's tools
(initialize -> tools/list, via probe()) but had no way to CALL one: zero
occurrences of tools/call in the module. A registered server's tools were
visible in `mcp tools` and callable by nothing, so registering email_mcp
and sensei_mcp_server changed what the agent could *see* and nothing about
what it could *do*.

These run against a stub stdio MCP server written to a temp dir, never a
real one — a test that reads a live Gmail inbox is not a test.
"""

from __future__ import annotations

import sys

import pytest

STUB_SERVER = """
import json, sys

TOOLS = [
    {"name": "echo", "description": "echo text back",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "boom", "description": "always errors",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "iserror", "description": "returns isError in-band",
     "inputSchema": {"type": "object", "properties": {}}},
]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        out = {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub", "version": "0"}}}
    elif method == "tools/list":
        out = {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    elif method == "tools/call":
        name = (msg.get("params") or {}).get("name")
        args = (msg.get("params") or {}).get("arguments") or {}
        if name == "echo":
            out = {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "echo:" + str(args.get("text"))}]}}
        elif name == "boom":
            out = {"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32000, "message": "tool exploded"}}
        elif name == "iserror":
            out = {"jsonrpc": "2.0", "id": mid, "result": {
                "isError": True, "content": [{"type": "text", "text": "nope"}]}}
        else:
            out = {"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": f"no tool {name}"}}
    elif mid is not None:
        continue
    else:
        continue
    sys.stdout.write(json.dumps(out) + "\\n")
    sys.stdout.flush()
"""


BAD_SCHEMA_SERVER = STUB_SERVER.replace(
    '{"type": "object", "properties": {"text": {"type": "string"}}}',
    '"not-an-object"',
)


@pytest.fixture
def stub_catalog(tmp_path, monkeypatch):
    """A catalog containing one stub server, isolated from the real one."""
    import sensei_mcp_client as mcp

    server = tmp_path / "stub_mcp.py"
    server.write_text(STUB_SERVER)

    cat = tmp_path / "mcp"
    cat.mkdir()
    monkeypatch.setattr(mcp, "MCP_DIR", cat)
    monkeypatch.setattr(mcp, "CATALOG_PATH", cat / "servers.json")

    mcp.add_server("stub", f"{sys.executable} {server}")
    return mcp


# ── discovery still works ──


def test_stub_server_is_discovered(stub_catalog):
    entry = stub_catalog.get_server("stub")
    assert entry["valid"] is True
    assert "echo" in entry["tool_names"]


# ── the missing half: actually calling ──


def test_call_tool_reaches_the_server(stub_catalog):
    r = stub_catalog.call_tool("stub", "echo", {"text": "hi"})
    assert r["ok"] is True, r["error"]
    assert r["result"] == "echo:hi"
    assert r["server"] == "stub" and r["tool"] == "echo"


def test_call_tool_returns_content_not_the_envelope(stub_catalog):
    """The model wants the text, not MCP's content-block envelope."""
    r = stub_catalog.call_tool("stub", "echo", {"text": "x"})
    assert isinstance(r["result"], str)
    assert "content" not in r["result"]


def test_jsonrpc_error_is_surfaced(stub_catalog):
    r = stub_catalog.call_tool("stub", "boom", {})
    assert r["ok"] is False
    assert "exploded" in r["error"]


def test_tool_reported_iserror_is_a_failure(stub_catalog):
    """MCP reports tool failure in-band, not as a JSON-RPC error."""
    r = stub_catalog.call_tool("stub", "iserror", {})
    assert r["ok"] is False
    assert "nope" in str(r["result"])


# ── refusals ──


def test_unknown_server_fails_cleanly(stub_catalog):
    r = stub_catalog.call_tool("nope", "echo", {})
    assert r["ok"] is False
    assert "no such MCP server" in r["error"]


def test_unknown_tool_is_refused_before_spawning(stub_catalog):
    r = stub_catalog.call_tool("stub", "not_a_tool", {})
    assert r["ok"] is False
    assert "does not expose" in r["error"]


def test_disabled_server_is_refused(stub_catalog):
    stub_catalog.set_enabled("stub", False)
    r = stub_catalog.call_tool("stub", "echo", {})
    assert r["ok"] is False
    assert "not enabled" in r["error"]


def test_call_tool_never_raises_on_junk(stub_catalog):
    for args in (None, "notadict", 123):
        r = stub_catalog.call_tool("stub", "echo", args)
        assert r["ok"] in (True, False)


# ── MCP_CALL validation ──


def test_validator_accepts_a_well_formed_call(stub_catalog):
    import action_validation as av

    res = av.validate_action({"kind": "MCP_CALL", "target": 'stub echo {"text": "hi"}'})
    assert res.ok, res.reason


@pytest.mark.parametrize(
    "target,fragment",
    [
        ("stub not_a_tool {}", "no tool named"),
        ("nosuch tool {}", "no such MCP server"),
        ("stub echo {bad json", "not valid JSON"),
        ("stub echo [1,2]", "must be a JSON object"),
        ("stub", "needs `<server> <tool>"),
    ],
)
def test_validator_rejects_malformed_calls(stub_catalog, target, fragment):
    import action_validation as av

    res = av.validate_action({"kind": "MCP_CALL", "target": target})
    assert res.ok is False
    assert fragment in res.reason, res.reason


# ── dispatch through master_ai ──


def test_run_mcp_call_spec_feeds_result_into_history():
    """A successful call must land in history as [MCP RESULT]."""
    import master_ai

    called = {}

    class _Fake:
        @staticmethod
        def call_tool(server, tool, args):
            called.update(server=server, tool=tool, args=args)
            return {"ok": True, "result": "the answer", "error": ""}

    import sensei_mcp_client

    original = sensei_mcp_client.call_tool
    sensei_mcp_client.call_tool = _Fake.call_tool
    try:
        history = []
        master_ai._run_mcp_call_spec('stub echo {"text": "hi"}', history)
    finally:
        sensei_mcp_client.call_tool = original

    assert called == {"server": "stub", "tool": "echo", "args": {"text": "hi"}}
    assert any("[MCP RESULT]" in m["content"] for m in history)
    assert any("the answer" in m["content"] for m in history)


def test_failed_call_tells_the_model_it_did_not_run():
    """The whole point: a blocked call must not read as a completed step."""
    import master_ai
    import sensei_mcp_client

    def _fail(server, tool, args):
        return {"ok": False, "result": None, "error": "server exploded"}

    original = sensei_mcp_client.call_tool
    sensei_mcp_client.call_tool = _fail
    try:
        history = []
        master_ai._run_mcp_call_spec("stub echo {}", history)
    finally:
        sensei_mcp_client.call_tool = original

    assert history, "a failed call produced no feedback at all"
    text = history[-1]["content"]
    assert "[MCP BLOCKED]" in text
    assert "Do not report this step as done." in text


def test_dispatch_never_raises_on_a_malformed_spec():
    import master_ai

    history = []
    for bad in ("", "   ", "onlyoneword", None):
        master_ai._run_mcp_call_spec(bad, history)  # must not raise


def test_a_server_with_a_bad_tool_schema_is_left_disabled(tmp_path, monkeypatch):
    """Probe-before-trust: one malformed schema must not enable the server.

    The stub here declares a tool whose inputSchema is a string rather than
    an object. The catalog must record the reason and leave the server
    disabled, so the agent cannot call tools from a server whose contract it
    never validated.
    """
    import sensei_mcp_client as mcp

    server = tmp_path / "bad_mcp.py"
    server.write_text(BAD_SCHEMA_SERVER)
    cat = tmp_path / "mcp"
    cat.mkdir()
    monkeypatch.setattr(mcp, "MCP_DIR", cat)
    monkeypatch.setattr(mcp, "CATALOG_PATH", cat / "servers.json")

    result = mcp.add_server("bad", f"{sys.executable} {server}")
    entry = mcp.get_server("bad")

    assert result["ok"] is False
    assert entry["valid"] is False
    assert entry["enabled"] is False
    assert any("inputSchema" in p for p in entry["problems"]), entry["problems"]

    # And it cannot be called.
    r = mcp.call_tool("bad", "echo", {})
    assert r["ok"] is False
    assert "not enabled" in r["error"]


# ── a falsy-failure trap that silently disabled every validator ──


def test_a_failing_validation_result_is_not_treated_as_success(stub_catalog):
    """ValidationResult.__bool__ returns ok, so a FAILED result is falsy.

    `_common_shape_checks` returns a failure object, and the call sites were
    written `if bad: return bad` — which never fired, because the failure is
    falsy. Empty and malformed payloads therefore fell straight through to
    the kind-specific checks instead of being rejected by the shared ones.
    Asserted across every validator that uses the shared check, not just
    MCP_CALL, because the bug was in all four.
    """
    import action_validation as av

    # The trap itself: a failure must be truthy-checked against None.
    failure = av.ValidationResult(ok=False, reason="x")
    assert bool(failure) is False, "premise of the bug"
    assert (
        failure is not None
    ) is True, "so `if failure is not None` is the correct guard"

    for kind in ("RUN", "RUNTERM", "READ", "MCP_CALL"):
        res = av.validate_action({"kind": kind, "target": ""})
        assert res.ok is False, f"{kind} accepted an empty payload"
        assert "empty" in res.reason, (kind, res.reason)

    for kind in ("RUN", "READ"):
        res = av.validate_action({"kind": kind, "target": "   "})
        assert res.ok is False, f"{kind} accepted a whitespace payload"
