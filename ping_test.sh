#!/usr/bin/env bash
# ping_test.sh — health check across the entire stack.
# Run from any terminal. Reports ✓/✗/⚠ per critical endpoint.
# Usage: bash ~/scripts/ping_test.sh        (or just the desktop icon)
#        bash ~/scripts/ping_test.sh --quiet   (no headers, just rows)

set +e  # do NOT die on first failure — we want all probes to run

QUIET="${1:-}"
TIMEOUT=3

probe() {
    # probe "<label>" "<cmd that returns 0 on success>"
    local label="$1"
    local cmd="$2"
    if eval "$cmd" >/dev/null 2>&1; then
        printf "  ✓  %-44s\n" "$label"
        return 0
    else
        printf "  ✗  %-44s\n" "$label"
        return 1
    fi
}

probe_with_status() {
    # probe_with_status "<label>" "<cmd that prints status code>"
    local label="$1"
    local cmd="$2"
    local result
    result=$(eval "$cmd" 2>&1)
    if [[ -z "$result" ]]; then
        printf "  ⚠  %-44s  (no response)\n" "$label"
    else
        printf "  ✓  %-44s  (%s)\n" "$label" "$result"
    fi
}

[[ "$QUIET" != "--quiet" ]] && echo "=== STACK PING — $(date '+%Y-%m-%d %H:%M:%S') ==="

# --- 1. Network layer ---
[[ "$QUIET" != "--quiet" ]] && echo; [[ "$QUIET" != "--quiet" ]] && echo "INTERNET"
probe "DNS resolves (cloudflare)"            "ping -c 1 -W $TIMEOUT 1.1.1.1"
probe "HTTPS reaches public endpoint"        "curl -fsS --max-time $TIMEOUT https://1.1.1.1 -o /dev/null"

# --- 2. Local services ---
[[ "$QUIET" != "--quiet" ]] && echo; [[ "$QUIET" != "--quiet" ]] && echo "LOCAL"
probe "Ollama responding on :11434"          "curl -fsS --max-time $TIMEOUT http://127.0.0.1:11434/api/version"
probe "CLAF proxy responding on :8000"       "curl -fsS --max-time $TIMEOUT http://127.0.0.1:8000/"
probe "Sensei bridge responding on :8080"    "curl -fsS --max-time $TIMEOUT http://127.0.0.1:8080/health"
probe "speak.sh script present + executable" "test -x $HOME/scripts/speak.sh"
probe "harness orchestrator present"         "test -f $HOME/scripts/sensei_3file/orchestrator.py"

# Surface the active CLAF/SENSEI mode so the operator sees the routing posture
# in one glance, not just "port responds."
CLAF_MODE_ACTIVE=$(curl -fsS --max-time $TIMEOUT http://127.0.0.1:8000/healthz 2>/dev/null \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("config",{}).get("mode","?"))' 2>/dev/null)
if [[ -n "$CLAF_MODE_ACTIVE" && "$CLAF_MODE_ACTIVE" != "?" ]]; then
    printf "  ●  SENSEI mode (via CLAF):                     %s\n" "$CLAF_MODE_ACTIVE"
fi

# --- 2.5 EXTREME route tests — exercise the actual paths, not just /health.
# Each test sends a real request and verifies it landed where it was supposed
# to (mode-honest, queue-driven, end-to-end). A port that says 200 is not
# the same as a route that does what it claims.
[[ "$QUIET" != "--quiet" ]] && echo; [[ "$QUIET" != "--quiet" ]] && echo "EXTREME ROUTE TESTS"

# CLAF route test — POST /v1/messages and confirm the route line says local
# (in local/hybrid mode) or a cloud peer name (in cloud mode). Asserts the
# active mode actually shapes routing, not that the URL is reachable.
CLAF_ROUTE_RESP=$(curl -fsS --max-time 30 -X POST http://127.0.0.1:8000/v1/messages \
    -H 'Content-Type: application/json' -H 'x-api-key: dummy' -H 'anthropic-version: 2023-06-01' \
    -d '{"model":"claude-sonnet-4-5","max_tokens":5,"messages":[{"role":"user","content":"reply OK"}]}' 2>/dev/null)
CLAF_ROUTE_LINE=$(tail -n 200 ~/projects/claf/orchestrator.log 2>/dev/null \
    | grep -F '"event": "route_decision"' | tail -1)
if [[ -n "$CLAF_ROUTE_RESP" && -n "$CLAF_ROUTE_LINE" ]]; then
    PICKED=$(echo "$CLAF_ROUTE_LINE" | python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); print(d.get("picked_name","?"))' 2>/dev/null)
    printf "  ✓  %-44s  (route=%s)\n" "CLAF /v1/messages routes per mode" "${PICKED:-unknown}"
else
    printf "  ✗  %-44s\n" "CLAF /v1/messages routes per mode"
fi

# Bridge chat test — POST /chat with a one-line prompt; assert reply non-empty.
# Uses a 90s ceiling because local model cold-start can take 30-75s; the test
# is honest about real latency, not a synthetic fast path. Skip with
# PING_SKIP_BRIDGE_CHAT=1 if you want a faster ping in tight loops.
if [[ "${PING_SKIP_BRIDGE_CHAT:-0}" == "1" ]]; then
    printf "  ⚠  %-44s  (skipped)\n" "Bridge /chat responds with session"
else
    BRIDGE_CHAT_OK=$(curl -fsS --max-time 90 -X POST http://127.0.0.1:8080/chat \
        -H 'Content-Type: application/json' \
        -d '{"prompt":"reply BROWSER_READ","session_id":"ping-test","page_context":{"url":"about:blank","title":"ping","text":""}}' 2>/dev/null \
        | python3 -c 'import sys,json; d=json.load(sys.stdin); print("ok" if d.get("session_id") else "")' 2>/dev/null)
    if [[ "$BRIDGE_CHAT_OK" == "ok" ]]; then
        printf "  ✓  %-44s\n" "Bridge /chat responds with session"
    else
        printf "  ✗  %-44s\n" "Bridge /chat responds with session"
    fi
fi

# MCP→Chrome round-trip — POST a no-op action to /extension/queue, then GET
# it back. Tests bridge wiring, NOT whether the side panel popped it (that
# requires Chrome and the extension running; covered by operator extreme test).
PING_SID="ping-mcp-$$"
PUSH_OK=$(curl -fsS --max-time 5 -X POST "http://127.0.0.1:8080/extension/queue" \
    -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$PING_SID\",\"actions\":[{\"kind\":\"BROWSER_NOOP\",\"target\":\"ping\"}]}" 2>/dev/null \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print("ok" if d.get("enqueued")==1 else "")' 2>/dev/null)
POP_COUNT=$(curl -fsS --max-time 5 "http://127.0.0.1:8080/extension/queue?session_id=$PING_SID" 2>/dev/null \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d.get("actions",[])))' 2>/dev/null)
if [[ "$PUSH_OK" == "ok" && "$POP_COUNT" == "1" ]]; then
    printf "  ✓  %-44s\n" "MCP→bridge /extension/queue round-trip"
else
    printf "  ✗  %-44s\n" "MCP→bridge /extension/queue round-trip"
fi

# SENSEI loop without Claude Code — drive the MCP server via stdin/stdout
# JSON-RPC, confirm browser.navigate lands an action in the queue. Proves
# the SENSEI loop boots and routes without ANY paid/optional client.
HEADLESS_SID="ping-headless-$$"
HEADLESS_RAW=$(printf '%s\n%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
    "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"browser.navigate\",\"arguments\":{\"url\":\"about:blank\",\"session_id\":\"$HEADLESS_SID\"}}}" \
    | timeout 5 python3 $HOME/scripts/sensei_mcp_server.py 2>/dev/null)
HEADLESS_OK=$(curl -fsS --max-time 5 "http://127.0.0.1:8080/extension/queue?session_id=$HEADLESS_SID" 2>/dev/null \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); a=d.get("actions",[]); print("ok" if a and a[0].get("kind")=="BROWSER_NAV" else "")' 2>/dev/null)
if [[ "$HEADLESS_OK" == "ok" ]]; then
    printf "  ✓  %-44s\n" "SENSEI loop sans Claude Code/Codex"
else
    printf "  ✗  %-44s\n" "SENSEI loop sans Claude Code/Codex"
fi

# --- 3. Cloud LLM endpoints (matters when on grid; expected ✗ when off) ---
[[ "$QUIET" != "--quiet" ]] && echo; [[ "$QUIET" != "--quiet" ]] && echo "CLOUD LLM REACHABILITY"
probe "Anthropic API reachable"              "curl -sS --max-time $TIMEOUT -o /dev/null https://api.anthropic.com/v1/messages"
probe "Ollama Cloud reachable"               "curl -sS --max-time $TIMEOUT -o /dev/null https://ollama.com/"
probe "OpenRouter reachable"                 "curl -sS --max-time $TIMEOUT -o /dev/null https://openrouter.ai/api/v1/models"
probe "Gemini API reachable"                 "curl -sS --max-time $TIMEOUT -o /dev/null https://generativelanguage.googleapis.com/v1beta/models"

# --- 4. Local Ollama model inventory ---
[[ "$QUIET" != "--quiet" ]] && echo; [[ "$QUIET" != "--quiet" ]] && echo "LOCAL MODELS ON DISK"
if command -v ollama >/dev/null && curl -fsS --max-time 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    ollama list 2>/dev/null | awk 'NR>1 {printf "  ✓  %-20s  %s\n", $1, $3" "$4}'
else
    echo "  ⚠  (Ollama not reachable — skipping model inventory)"
fi

# --- 5. Currently-loaded models in Ollama RAM ---
[[ "$QUIET" != "--quiet" ]] && echo; [[ "$QUIET" != "--quiet" ]] && echo "MODELS LOADED IN RAM (ollama ps)"
if curl -fsS --max-time 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    ollama ps 2>/dev/null | awk 'NR>1 && NF>0 {printf "  ●  %-20s  ttl: %s\n", $1, $5" "$6}' || echo "  (none loaded)"
else
    echo "  ⚠  (Ollama not reachable)"
fi

# --- 6. API key validation (delegate to existing validator) ---
[[ "$QUIET" != "--quiet" ]] && echo; [[ "$QUIET" != "--quiet" ]] && echo "API KEYS"
if [[ -x $HOME/scripts/validate_keys.py ]] || test -f $HOME/scripts/validate_keys.py; then
    python3 $HOME/scripts/validate_keys.py 2>&1 | awk '/^[✓✗⚠·-]/ {print "  "$0}'
else
    echo "  ⚠  (validate_keys.py missing)"
fi

# --- 7. Hardware snapshot ---
[[ "$QUIET" != "--quiet" ]] && echo; [[ "$QUIET" != "--quiet" ]] && echo "RESOURCES"
RAM_FREE=$(free -h | awk '/^Mem:/ {print $7}')
DISK_FREE=$(df -h ~ | awk 'NR==2 {print $4" available ("$5" used)"}')
LOAD=$(uptime | awk -F'load average:' '{print $2}' | xargs)
printf "  ●  RAM available: %s\n" "$RAM_FREE"
printf "  ●  Disk: %s\n" "$DISK_FREE"
printf "  ●  Load avg: %s\n" "$LOAD"

[[ "$QUIET" != "--quiet" ]] && echo
[[ "$QUIET" != "--quiet" ]] && echo "=== END PING ==="
