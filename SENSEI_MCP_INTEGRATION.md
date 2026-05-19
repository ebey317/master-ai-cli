# Sensei → MCP Wire-Up Design

**Status:** Design complete 2026-05-18 (extended session). Implementation deferred to fresh-token session. SDK verified working.

**Goal:** Give Sensei (`master_ai.py`) the ability to call MCP tools — the architectural move that lets Sensei stand in for Claude Code on browser/file/external work, per [[project_master_ai_architecture_plain_terms]] and [[project_convergence_2026_05_18]].

---

## Context — what already exists

1. **`REMOTE_MCP` directive is documented** in `master_ai.py` at line 11407 (prompt text only — model is TOLD about it). Format:
   ```
   REMOTE_MCP: {"server":"name-or-url","method":"tools/list"|"tools/call","params":{...}}
   ```
2. **No parser exists** for `REMOTE_MCP` — grep finds only the two prompt-text mentions (lines 11320 + 11407). No `_remote_mcp_specs_from_reply()`, no `_execute_remote_mcp()`.
3. **MCP Python SDK installed** as of 2026-05-18: `mcp-1.27.1` + deps. Verified import: `from mcp import ClientSession, StdioServerParameters; from mcp.client.stdio import stdio_client` → "mcp SDK ok".
4. **Pattern to copy: `RUN_SKILL`** — `master_ai.py:8985`. Two-function shape: `_run_skill_specs_from_reply(reply)` extracts directive lines, `_parse_run_skill_payload(payload)` validates the JSON. Then `process_reply()` consumes the specs.

## Architecture (what to build)

### File: `master_ai.py` (modifications)

#### 1. Server config loader (~30 lines, new function near top)

```python
def _load_mcp_servers():
    """Load MCP server configs from ~/.config/master_ai/mcp_servers.json.

    Schema:
      {
        "servers": {
          "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/elijah"],
            "env": {}
          },
          "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "<from env>"}
          }
        }
      }

    Returns dict[name → config]. Empty dict if config missing.
    """
```

Resolve `$ENV_VAR` substitution in env values so PATs don't sit in the JSON. Path: `~/.config/master_ai/mcp_servers.json`. Create dir if missing.

#### 2. Parser: `_remote_mcp_specs_from_reply(reply)` (~25 lines, near line 8985 with RUN_SKILL)

Mirrors `_run_skill_specs_from_reply`. Walks reply lines, finds `REMOTE_MCP:` directives via `_real_directive_line(line, "REMOTE_MCP")`, splits on the colon, JSON-parses the payload, validates shape (`server`, `method`, `params?`). Returns list of dicts.

#### 3. Executor: `_execute_remote_mcp(spec)` (~80 lines, near line 9100 with skill dispatchers)

```python
def _execute_remote_mcp(spec):
    """Connect to an MCP server (stdio-based), call the requested method,
    return the result as a string for history injection.

    spec: {"server": "filesystem", "method": "tools/call",
           "params": {"name": "read_file", "arguments": {"path": "/tmp/x"}}}

    Returns a formatted string to inject back into the model's history.
    On error, returns "[REMOTE_MCP_ERROR] <reason>" — same shape as
    other directive failures so retry logic sees it.
    """
    server_name = spec.get("server", "")
    method = spec.get("method", "tools/call")
    params = spec.get("params") or {}

    servers = _load_mcp_servers()
    cfg = servers.get(server_name)
    if not cfg:
        return f"[REMOTE_MCP_ERROR] unknown server: {server_name}"

    # Run async MCP call in a fresh event loop (master_ai is sync).
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _do():
        sp = StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args", []),
            env={**os.environ, **cfg.get("env", {})},
        )
        async with stdio_client(sp) as (read, write):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                if method == "tools/list":
                    res = await sess.list_tools()
                    return [(t.name, t.description) for t in res.tools]
                elif method == "tools/call":
                    name = params.get("name")
                    args = params.get("arguments") or {}
                    res = await sess.call_tool(name, args)
                    return [c.text for c in res.content if hasattr(c, "text")]
                else:
                    return [f"unsupported method: {method}"]

    try:
        result = asyncio.run(_do())
        return f"[REMOTE_MCP_RESULT] {server_name}/{method}: {result}"
    except Exception as e:
        return f"[REMOTE_MCP_ERROR] {type(e).__name__}: {e}"
```

#### 4. Wire into `process_reply()` (line 9184)

After the existing RUN_SKILL handling and before the legacy directive dispatch:

```python
# REMOTE_MCP — call configured MCP servers
mcp_specs = _remote_mcp_specs_from_reply("\n".join(lines))
for spec in mcp_specs:
    result = _execute_remote_mcp(spec)
    history.append({"role": "user", "content": result})
    # Strip the directive line from `lines` so legacy dispatch doesn't re-process
    lines = [l for l in lines if not _real_directive_line(l, "REMOTE_MCP")]
```

#### 5. Audit log entry (optional but matches existing pattern)

Each REMOTE_MCP dispatch writes one line to `~/.master_ai_router_metrics.jsonl` with `{ts, route: "remote_mcp", server, method, latency_ms, success}` per the existing metrics shape.

### File: `~/.config/master_ai/mcp_servers.json` (new, user-editable)

Initial config with two servers:

```json
{
  "servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/elijah"],
      "env": {}
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "$GITHUB_TOKEN"}
    }
  }
}
```

GitHub server requires a PAT in `$GITHUB_TOKEN` env var (operator generates at github.com/settings/tokens, scopes: `repo` + `workflow`).

### File: regression test (new)

`~/scripts/test_master_ai_mcp.py` — three tests:

1. `test_remote_mcp_parser_extracts_specs` — feed reply text with REMOTE_MCP line, assert spec dict shape
2. `test_remote_mcp_unknown_server_error` — call with unconfigured server, assert error
3. `test_remote_mcp_filesystem_list_tools` — actually connect to filesystem MCP server, call `tools/list`, assert tool names contain `read_file` and `write_file`

(Test #3 requires `@modelcontextprotocol/server-filesystem` installable via npx — happens on first call.)

## Execution sequence for the next session

1. Create `~/.config/master_ai/mcp_servers.json` with filesystem server only (skip GitHub until PAT exists)
2. Write the four functions in `master_ai.py` per spec above
3. Wire into `process_reply()`
4. Write the test file, run, confirm green
5. Test live: have Sensei emit `REMOTE_MCP: {"server":"filesystem","method":"tools/list"}` via direct user prompt, verify the result lands in history
6. Test live: have Sensei emit `REMOTE_MCP: {"server":"filesystem","method":"tools/call","params":{"name":"read_file","arguments":{"path":"/tmp/test.txt"}}}` and verify
7. Commit: `feat(sensei): wire REMOTE_MCP executor — Sensei now calls MCP tools via Anthropic SDK`
8. Push, mark task #11 completed

## Time estimate for fresh-token session

- ~30-60 min focused implementation (4 functions + wiring + config)
- ~20 min testing (parser unit + live integration)
- ~10 min commit/push/memory update

Total: ~1.5 hours of focused execution. Far less if no edge cases bite.

## What this unlocks

Once landed: Sensei can read files via MCP (replaces some `READ:` use), call GitHub via MCP (replaces some `RUN: gh ...`), drive browser via MCP if/when a browser MCP server exists (the sensei_extension already plays this role natively, so MCP browser is later). The architectural promise of [[project_master_ai_architecture_plain_terms]] becomes code reality.

Sensei stops needing Claude Code as a sibling agent for tool-driven work. The "we don't need BC" realization from tonight ([[project_convergence_2026_05_18]]) becomes literally true.

## Memory references

- [[project_master_ai_architecture_plain_terms]] — the why
- [[project_convergence_2026_05_18]] — tonight's moment of clarity
- [[project_anthropic_brand_ambassador_angle]] — partnership implication
- [[feedback_brand_voice_patterns]] — voice for future commit messages on this work
- [[feedback_work_cadence_commit_push]] — apply commit-cadence rule per implementation milestone
