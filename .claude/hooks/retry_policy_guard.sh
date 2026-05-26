#!/usr/bin/env bash
# retry_policy_guard.sh — Schema enforcement v1.1
# Registered as: PostToolUse hook in ~/.claude/settings.json
# (NOT PostToolUseFailure — that event does not exist in Claude Code)
#
# Receives on stdin: JSON with tool_name, tool_input, tool_response
# Exit codes: 0=ALLOW, 1=BLOCK_RETRY, 2=BLOCK_FATAL, 3=INTERNAL_ERROR (fail-closed)
#
# Kill switch:    touch ~/.claude/.retry_kill_switch  (v1.1)
# Legacy switch:  touch /tmp/retry_policy_disabled    (v1.0 compat)
# Reset state:    rm ~/.claude/.retry_state.json
# Op namespace:   echo "name" > ~/.claude/.current_operation
#
# Locked 2026-05-23 — v1.1 hardening pass.
set -euo pipefail

# ── Constants ──────────────────────────────────────────────────────────────
readonly HC="$HOME/.claude"
readonly POLICY_YAML="$HC/retry_policy.yaml"
readonly POLICY_JSON="$HC/.retry_policy.json"
readonly POLICY_SUM="$HC/.retry_policy.json.sha256"
readonly STATE="$HC/.retry_state.json"
readonly STATE_LOCK="$HC/.retry_state.json.lock"
readonly HEALTH="$HC/.hook_health.json"
readonly KILL_SWITCH="$HC/.retry_kill_switch"
readonly LOG="$HC/retry_policy.log"

readonly ALLOW=0
readonly BLOCK_RETRY=1
readonly BLOCK_FATAL=2
readonly INTERNAL_ERROR=3

TS=$(date '+%Y-%m-%d %H:%M:%S')
readonly TS

STATE_FRESH='{"schema_version":"1.1","operations":{},"global":{"consecutive_failures_any_tool":0,"circuit_state":"closed"}}'

# ── ERR trap — fail-closed on unexpected crash ─────────────────────────────
trap 'echo "[${TS}] INTERNAL_ERROR on line ${LINENO} — hook crashed, fail-closed (exit 3)" >> "$LOG"; exit $INTERNAL_ERROR' ERR

# ── Helpers ────────────────────────────────────────────────────────────────
write_heartbeat() {
    printf '{"pid":%d,"last_heartbeat":"%s","schema_version":"1.1"}\n' \
        "$$" "$TS" > "${HEALTH}.tmp" \
        && mv -f "${HEALTH}.tmp" "$HEALTH" 2>/dev/null || true
}

emit_block() {
    local exit_code="$1"
    local reason="$2"
    # Escape embedded double-quotes for valid JSON
    reason="${reason//\\/\\\\}"
    reason="${reason//\"/\\\"}"
    printf '{"decision":"block","reason":"%s"}\n' "$reason"
    exit "$exit_code"
}

# ── 1. Kill switch (v1.1 path + v1.0 legacy) ──────────────────────────────
if [[ -f "$KILL_SWITCH" ]]; then
    echo "[$TS] kill_switch=ACTIVE ($KILL_SWITCH) — enforcement suspended" >> "$LOG"
    exit $BLOCK_FATAL
fi
if [[ -f /tmp/retry_policy_disabled ]]; then
    echo "[$TS] legacy kill_switch=ACTIVE (/tmp/retry_policy_disabled) — enforcement suspended" >> "$LOG"
    exit $ALLOW
fi

# ── 2. Dependency check — fail-closed if missing ──────────────────────────
for dep in jq flock sha256sum; do
    if ! command -v "$dep" >/dev/null 2>&1; then
        echo "[$TS] DEPENDENCY_MISSING: $dep" >> "$LOG"
        emit_block $BLOCK_FATAL "DEPENDENCY_MISSING: $dep not found. Install and retry. (enforcement: fail-closed)"
    fi
done

# ── 3. Read stdin ──────────────────────────────────────────────────────────
INPUT="$(cat)"
TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null || echo "")
[[ -z "$TOOL_NAME" ]] && { echo "[$TS] SKIP no tool_name in payload" >> "$LOG"; exit $ALLOW; }

# Detect failure vs success (PostToolUse sees both — must classify from payload)
IS_ERROR=$(printf '%s' "$INPUT" | jq -r '
    if .tool_response.is_error == true then "true"
    elif (.error // "") != "" then "true"
    else "false"
    end
' 2>/dev/null || echo "false")

# Extract response text for content-based failure detection and hash
RESPONSE_TEXT=$(printf '%s' "$INPUT" | jq -r '
    (.tool_response.content // [] | map(select(.type == "text")) | .[0].text // "") //
    (.tool_response | if type == "string" then . else "" end) //
    ""
' 2>/dev/null | head -c 600 || echo "")

# Pull explicit error string
ERROR_STR=$(printf '%s' "$INPUT" | jq -r '
    (.error // .tool_response.error // "") | if . == null then "" else . end
' 2>/dev/null || echo "")
# Merge with response text if no explicit error
[[ -z "$ERROR_STR" && -n "$RESPONSE_TEXT" ]] && ERROR_STR="$RESPONSE_TEXT"

HTTP_CODE=$(printf '%s' "$INPUT" | jq -r '.http_status // .tool_response.status // ""' 2>/dev/null || echo "")

# Content-based failure override: observability strings in "successful" responses
if echo "$RESPONSE_TEXT" | grep -qE 'BROWSER_SCREENSHOT must be handled|js_eval returned .failure.|interactive_elements.*truncated'; then
    IS_ERROR="true"
fi

# ── 4. Auto-compile YAML → JSON if stale; verify checksum ─────────────────
if [[ -f "$POLICY_YAML" ]]; then
    if [[ ! -f "$POLICY_JSON" ]] || [[ "$POLICY_YAML" -nt "$POLICY_JSON" ]]; then
        echo "[$TS] POLICY_STALE — recompiling" >> "$LOG"
        if ! bash "$(dirname "$(readlink -f "$0")")/_compile_policy.sh" >/dev/null 2>&1; then
            echo "[$TS] COMPILE_ERROR — fail-closed" >> "$LOG"
            emit_block $BLOCK_FATAL "COMPILE_ERROR: retry_policy.yaml failed to compile. Fix YAML and run _compile_policy.sh manually."
        fi
    fi
    if [[ -f "$POLICY_SUM" && -f "$POLICY_JSON" ]]; then
        ACTUAL_SUM=$(sha256sum "$POLICY_JSON" | cut -d' ' -f1)
        EXPECTED_SUM=$(cat "$POLICY_SUM" | cut -d' ' -f1)
        if [[ "$ACTUAL_SUM" != "$EXPECTED_SUM" ]]; then
            echo "[$TS] CHECKSUM_MISMATCH: $POLICY_JSON tampered outside _compile_policy.sh" >> "$LOG"
            emit_block $BLOCK_FATAL "CHECKSUM_MISMATCH: .retry_policy.json was modified outside _compile_policy.sh. Run _compile_policy.sh to regenerate."
        fi
    fi
fi

MAX_ATTEMPTS=3
[[ -f "$POLICY_JSON" ]] && \
    MAX_ATTEMPTS=$(jq -r '.retry_policy.max_attempts // 3' "$POLICY_JSON" 2>/dev/null || echo "3")

# ── 5. Tool alias normalization (amendment #10) ───────────────────────────
# Bash 4+ associative array; maps raw tool name → canonical group key
declare -A TOOL_ALIASES
TOOL_ALIASES=(
    ["mcp__sensei__click"]="browser_click"
    ["mcp__sensei__fill"]="browser_fill"
    ["mcp__sensei__screenshot"]="browser_screenshot"
    ["mcp__sensei__js_eval"]="browser_eval"
    ["mcp__sensei__read"]="browser_read"
    ["mcp__sensei__browse"]="browser_navigate"
    ["mcp__claude-in-chrome__computer"]="browser_interact"
    ["mcp__claude-in-chrome__javascript_tool"]="browser_eval"
    ["mcp__claude-in-chrome__read_page"]="browser_read"
    ["mcp__claude-in-chrome__navigate"]="browser_navigate"
    ["mcp__claude-in-chrome__find"]="browser_read"
    ["mcp__claude-in-chrome__form_input"]="browser_fill"
    ["mcp__claude-in-chrome__browser_batch"]="browser_interact"
)
CANONICAL_TOOL="${TOOL_ALIASES[$TOOL_NAME]:-$TOOL_NAME}"

# ── 6. Browser tool detection (for pre-flight check) ─────────────────────
IS_BROWSER_TOOL=false
[[ "$TOOL_NAME" =~ ^mcp__(sensei|claude-in-chrome)__ ]] && IS_BROWSER_TOOL=true

# ── 7. Operation namespace ─────────────────────────────────────────────────
OP_ID=$(cat "$HC/.current_operation" 2>/dev/null | tr -dc '[:alnum:]-_.' | head -c 64 || echo "")
[[ -z "$OP_ID" ]] && OP_ID="default"

# ── 8. Acquire flock on state file; self-heal on corruption ───────────────
exec 9>"$STATE_LOCK"
flock -x 9
trap 'flock -u 9 2>/dev/null || true' EXIT  # always release on exit

if [[ ! -f "$STATE" ]]; then
    printf '%s\n' "$STATE_FRESH" > "$STATE"
elif ! jq empty "$STATE" 2>/dev/null; then
    echo "[$TS] STATE_CORRUPTION_RECOVERY — resetting $STATE to safe defaults" >> "$LOG"
    printf '%s\n' "$STATE_FRESH" > "$STATE"
fi

# ── 9. Compute hashes ──────────────────────────────────────────────────────
RESPONSE_HASH=$(printf '%s' "$RESPONSE_TEXT" | sha256sum | cut -c1-16 || echo "0000000000000000")
ARGS_HASH=$(printf '%s' "$INPUT" | jq -r '.tool_input // {}' 2>/dev/null | sha256sum | cut -c1-12 || echo "000000000000")

# ── 10. SUCCESS PATH ──────────────────────────────────────────────────────
if [[ "$IS_ERROR" == "false" ]]; then
    # SEMANTIC_NO_OP: identical response hash on consecutive calls (browser tools only)
    LAST_HASH=$(jq -r --arg op "$OP_ID" --arg t "$CANONICAL_TOOL" \
        '.operations[$op].tools[$t].last_response_hash // ""' "$STATE" 2>/dev/null || echo "")

    if [[ -n "$LAST_HASH" && "$LAST_HASH" == "$RESPONSE_HASH" && "$IS_BROWSER_TOOL" == "true" && "$RESPONSE_TEXT" != "" ]]; then
        echo "[$TS] SEMANTIC_NO_OP op=$OP_ID tool=$CANONICAL_TOOL — identical hash $RESPONSE_HASH twice" >> "$LOG"
        jq --arg op "$OP_ID" --arg t "$CANONICAL_TOOL" --arg rh "$RESPONSE_HASH" --arg ts "$TS" \
            '.operations[$op] //= {tools:{},circuit_state:"closed"}
            | .operations[$op].tools[$t] //= {}
            | .operations[$op].tools[$t].consecutive_failures = 0
            | .operations[$op].tools[$t].last_response_hash = $rh
            | .operations[$op].tools[$t].last_noop_ts = $ts' \
            "$STATE" > "${STATE}.tmp" && mv -f "${STATE}.tmp" "$STATE" 2>/dev/null || true
        write_heartbeat
        emit_block $BLOCK_FATAL "STOP: SEMANTIC_NO_OP on $CANONICAL_TOOL — identical response hash ($RESPONSE_HASH) on consecutive calls in op=$OP_ID. State unchanged. Apply fallback_order: switch_tool → switch_protocol → operator_eyes → operator_hands."
    fi

    # Normal success — read prior failure count, reset counter, log SUCCESS for calibration
    PRIOR_FAILS=$(jq -r --arg op "$OP_ID" --arg t "$CANONICAL_TOOL" \
        '.operations[$op].tools[$t].consecutive_failures // 0' "$STATE" 2>/dev/null || echo "0")
    ATTEMPT_N=$(( PRIOR_FAILS + 1 ))
    echo "[$TS] SUCCESS op=$OP_ID tool=$TOOL_NAME canonical=$CANONICAL_TOOL attempt=$ATTEMPT_N" >> "$LOG"

    jq --arg op "$OP_ID" --arg t "$CANONICAL_TOOL" --arg rh "$RESPONSE_HASH" --arg ts "$TS" \
        '.operations[$op] //= {tools:{},circuit_state:"closed"}
        | .operations[$op].tools[$t] //= {}
        | .operations[$op].tools[$t].consecutive_failures = 0
        | .operations[$op].tools[$t].last_response_hash = $rh
        | .operations[$op].tools[$t].last_success_ts = $ts
        | .global.consecutive_failures_any_tool = 0' \
        "$STATE" > "${STATE}.tmp" && mv -f "${STATE}.tmp" "$STATE" 2>/dev/null || true

    write_heartbeat
    exit $ALLOW
fi

# ── 11. Pre-flight: block retry immediately if observability is broken ─────
# After a browser tool failure, detect known-dead channels so the NEXT attempt
# is blocked before it wastes a retry slot.
if [[ "$IS_BROWSER_TOOL" == "true" ]]; then
    if echo "$ERROR_STR" | grep -qE 'BROWSER_SCREENSHOT must be handled'; then
        echo "[$TS] PRE_FLIGHT_FAIL op=$OP_ID tool=$CANONICAL_TOOL — screenshot bridge broken" >> "$LOG"
        write_heartbeat
        emit_block $BLOCK_FATAL "STOP: PRE_FLIGHT_FAIL — screenshot bridge broken on $TOOL_NAME. OBSERVABILITY_FAILURE: retry forbidden before bridge is restored. Apply fallback_order: switch_tool → switch_protocol → operator_eyes → operator_hands."
    fi
fi

# ── 12. Classify failure ──────────────────────────────────────────────────
CLASS="UNKNOWN"
if echo "$ERROR_STR" | grep -qE 'BROWSER_SCREENSHOT must be handled|js_eval returned .failure.|interactive_elements.*truncated'; then
    CLASS="OBSERVABILITY_FAILURE"
elif [[ "$HTTP_CODE" =~ ^(500|502|503|504)$ ]]; then
    CLASS="TRANSIENT"
elif [[ "$HTTP_CODE" =~ ^(429|509)$ ]]; then
    CLASS="INTERMITTENT"
elif [[ "$HTTP_CODE" =~ ^(401|403)$ ]]; then
    CLASS="AUTH"
elif [[ "$HTTP_CODE" =~ ^(400|404|422)$ ]]; then
    CLASS="PERMANENT"
elif echo "$ERROR_STR" | grep -qiE 'rate.?limit|throttl'; then
    CLASS="INTERMITTENT"
elif echo "$ERROR_STR" | grep -qiE 'ECONNRESET|ETIMEDOUT|EAI_AGAIN'; then
    CLASS="TRANSIENT"
elif echo "$ERROR_STR" | grep -qiE 'not.?found'; then
    CLASS="PERMANENT"
elif echo "$ERROR_STR" | grep -qiE 'validation.?error'; then
    CLASS="PERMANENT"
elif echo "$ERROR_STR" | grep -qiE 'token.?expired|unauthorized|forbidden'; then
    CLASS="AUTH"
elif echo "$ERROR_STR" | grep -qiE 'browser.?extension.?is.?not.?connected'; then
    CLASS="OBSERVABILITY_FAILURE"
fi

# ── 13. Increment failure counter (flock held) ────────────────────────────
NEW_STATE=$(jq --arg op "$OP_ID" --arg t "$CANONICAL_TOOL" \
    --arg ah "$ARGS_HASH" --arg ts "$TS" --arg cls "$CLASS" '
  .operations[$op] //= {tools:{}, circuit_state:"closed"}
  | .operations[$op].tools[$t] //= {consecutive_failures:0, last_args_hash:"", last_class:""}
  | (if .operations[$op].tools[$t].last_args_hash == $ah
     then .operations[$op].tools[$t].consecutive_failures += 1
     else .operations[$op].tools[$t].consecutive_failures = 1
          | .operations[$op].tools[$t].last_args_hash = $ah
     end)
  | .operations[$op].tools[$t].last_failure_ts = $ts
  | .operations[$op].tools[$t].last_class = $cls
  | .global.consecutive_failures_any_tool += 1
' "$STATE" 2>/dev/null || printf '%s' "$STATE_FRESH")

printf '%s\n' "$NEW_STATE" > "${STATE}.tmp" && mv -f "${STATE}.tmp" "$STATE"

FAILS=$(printf '%s' "$NEW_STATE" | jq -r --arg op "$OP_ID" --arg t "$CANONICAL_TOOL" \
    '.operations[$op].tools[$t].consecutive_failures // 1' 2>/dev/null || echo "1")

echo "[$TS] FAIL op=$OP_ID tool=$TOOL_NAME canonical=$CANONICAL_TOOL class=$CLASS count=$FAILS http=${HTTP_CODE:-none}" >> "$LOG"

write_heartbeat

# ── 14. Decide ────────────────────────────────────────────────────────────
case "$CLASS" in
    OBSERVABILITY_FAILURE)
        emit_block $BLOCK_FATAL \
            "STOP: OBSERVABILITY_FAILURE on $CANONICAL_TOOL. Cannot SEE result — blind retry forbidden (schema v1.1). Apply fallback_order: switch_tool → switch_protocol → operator_eyes → operator_hands."
        ;;
    PERMANENT)
        emit_block $BLOCK_FATAL \
            "STOP: PERMANENT failure on $CANONICAL_TOOL (http=${HTTP_CODE:-none}). Retry forbidden. Diagnose request construction."
        ;;
    UNKNOWN)
        emit_block $BLOCK_FATAL \
            "STOP: UNKNOWN error class on $CANONICAL_TOOL. Unclassified errors are non-retryable per schema v1.1. Error: ${ERROR_STR:0:200}"
        ;;
    AUTH)
        if [[ "$FAILS" -ge 2 ]]; then
            emit_block $BLOCK_FATAL \
                "STOP: AUTH failure persisted after refresh ($FAILS attempts) on $CANONICAL_TOOL. Hand off to operator for re-auth."
        fi
        ;;
    *)
        if [[ "$FAILS" -ge "$MAX_ATTEMPTS" ]]; then
            emit_block $BLOCK_FATAL \
                "STOP: $CANONICAL_TOOL hit max_attempts ($MAX_ATTEMPTS) in op=$OP_ID. 4th attempt forbidden. Apply fallback_order: switch_tool → switch_protocol → operator_eyes → operator_hands. Reset counters: rm $STATE OR echo NEW_OP > ~/.claude/.current_operation"
        fi
        ;;
esac

exit $ALLOW
