#!/usr/bin/env bash
# userpromptsubmit_inject.sh — Schema v1.1, Layer 0 Channel B
# Registered as: UserPromptSubmit hook in ~/.claude/settings.json
#
# Reads .hook_health.json + .retry_state.json and prepends a schema
# status line to the model's view of each user message.
# If heartbeat is stale >600s → escalates to [HEARTBEAT STALE] warning.
# Also fires a desktop notification + audio cue on FIRST message of each session.
#
# Exit 0 always (informational only — never blocks the user's message).
# Output format: JSON matching Claude Code's UserPromptSubmit hook spec.
set -euo pipefail

readonly HC="$HOME/.claude"
readonly HEALTH="$HC/.hook_health.json"
readonly STATE="$HC/.retry_state.json"
readonly KILL_SWITCH="$HC/.retry_kill_switch"
readonly LOG="$HC/retry_policy.log"

TS=$(date '+%Y-%m-%d %H:%M:%S')

# ── SESSION START NOTIFICATION ────────────────────────────────────────────────
# Read stdin to get session_id (Claude Code passes hook context via stdin JSON).
# We buffer it so the rest of the hook still works without stdin.
HOOK_INPUT="$(cat 2>/dev/null || true)"
SESSION_ID="$(printf '%s' "$HOOK_INPUT" | jq -r '.session_id // ""' 2>/dev/null || true)"
SESSION_STARTUP_NON_NEG=""
if [[ -n "$SESSION_ID" ]]; then
    SESSION_FLAG="/tmp/claude_session_notified_${SESSION_ID}"
    if [[ ! -f "$SESSION_FLAG" ]]; then
        touch "$SESSION_FLAG"
        # Visual desktop notification — appears in the notification tray
        command -v notify-send &>/dev/null && \
            DISPLAY="${DISPLAY:-:0}" notify-send \
                "🤖 Claude Online" \
                "Session ready. $(date '+%-I:%M %p')" \
                --urgency=normal \
                --expire-time=5000 \
                2>/dev/null & disown
        # Audio cue via speak.sh (background — don't block injection)
        [[ -x "$HOME/scripts/speak.sh" ]] && \
            "$HOME/scripts/speak.sh" "Claude is online." &>/dev/null & disown
        # Log the session start
        echo "$TS [SESSION_START] id=${SESSION_ID}" >> "$LOG" 2>/dev/null || true

        # ── NON-NEGOTIABLES — injected ONCE at session startup ────────────────
        SESSION_STARTUP_NON_NEG="[NON-NEGOTIABLES — STARTUP LOAD 2026-05-24] "
        SESSION_STARTUP_NON_NEG+="#1 MCP TABS ONLY: No bare Chrome tabs. No google-chrome <url>. Always tabs_create_mcp then navigate/javascript_tool. No exceptions. "
        SESSION_STARTUP_NON_NEG+="#2 NO EXTENSION DEPENDENCY: Stack must have full browser parity (tabs/JS/DOM/console/network/screenshots) via Playwright/CDP without Chrome extension. If extension breaks, stack stays up. "
        SESSION_STARTUP_NON_NEG+="#3 OPERATOR SEES AUTHENTICATED ACTIONS: Any tool using operator login (Canva/Drive/Gmail/Calendar/Todoist/sensei/secretary/HF/Base44/Indeed/ZipRecruiter/claude-in-chrome) MUST be operator-visible. Narrate before firing. Open relevant URL in visible tab BEFORE state-changing API calls. No headless changes. "
        SESSION_STARTUP_NON_NEG+="#4 RETRY HARD CAP: 3 attempts per (operation_id, tool). Any failure → fallback chain immediately: switch_tool→switch_protocol→operator_eyes→operator_hands. OBSERVABILITY_FAILURE+PERMANENT+UNKNOWN+REFLECTION_FAILURE = zero retries. "
        SESSION_STARTUP_NON_NEG+="#5 BROWSER VISIBILITY: If screenshot+js_eval+read ALL fail, STOP. Do not retry blind. No click-loops on invisible modals. Switch tool or hand off. "
        SESSION_STARTUP_NON_NEG+="#6 CAPABILITY POSTURE: Code+info = A1. Extension/terminal/multi-agent = weak. Lead with code and information. Do NOT spawn agents or drive browser/terminal flows unless explicitly told to act."
        # ─────────────────────────────────────────────────────────────────────
    fi
fi
# ─────────────────────────────────────────────────────────────────────────────

# If kill switch active, inject a clear notice and exit
if [[ -f "$KILL_SWITCH" ]]; then
    printf '{"hookSpecificReturn":{"additionalSystemPrompt":"[RETRY_SCHEMA] kill_switch=ACTIVE. Enforcement suspended. Remove %s to re-enable."}}\n' "$KILL_SWITCH"
    exit 0
fi

# Require jq — if missing, skip injection silently
if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

# ── Check hook heartbeat freshness ────────────────────────────────────────
ENFORCEMENT_STATUS="OK"
HEARTBEAT_AGE_MSG=""
if [[ -f "$HEALTH" ]]; then
    LAST_BEAT=$(jq -r '.last_heartbeat // ""' "$HEALTH" 2>/dev/null || echo "")
    if [[ -n "$LAST_BEAT" ]]; then
        # Convert to epoch seconds for age calculation
        LAST_TS=$(date -d "$LAST_BEAT" +%s 2>/dev/null || echo "0")
        NOW_TS=$(date +%s)
        AGE=$(( NOW_TS - LAST_TS ))
        if [[ "$AGE" -gt 600 ]]; then
            ENFORCEMENT_STATUS="STALE"
            HEARTBEAT_AGE_MSG=" (last beat ${AGE}s ago)"
        fi
    fi
else
    ENFORCEMENT_STATUS="NEVER_FIRED"
    HEARTBEAT_AGE_MSG=" (hook has never fired this session)"
fi

# ── Read active failure counters ──────────────────────────────────────────
OP_ID=$(cat "$HC/.current_operation" 2>/dev/null | tr -dc '[:alnum:]-_.' | head -c 64 || echo "")
[[ -z "$OP_ID" ]] && OP_ID="default"

ACTIVE_FAILURES=""
if [[ -f "$STATE" ]] && jq empty "$STATE" 2>/dev/null; then
    MAX_ATT=$(jq -r '.retry_policy.max_attempts // 3' "$HC/.retry_policy.json" 2>/dev/null || echo "3")
    FAIL_SUMMARY=$(jq -r --arg op "$OP_ID" --arg max "$MAX_ATT" '
        (.operations[$op].tools // {}) |
        to_entries |
        map(select(.value.consecutive_failures > 0)) |
        map("\(.key)=\(.value.consecutive_failures)/\($max | tonumber) (\(.value.last_class // "?"))") |
        if length > 0 then join(", ") else "" end
    ' "$STATE" 2>/dev/null || echo "")
    [[ -n "$FAIL_SUMMARY" ]] && ACTIVE_FAILURES=" | active_failures: $FAIL_SUMMARY"
fi

# ── Build injection message ───────────────────────────────────────────────
if [[ "$ENFORCEMENT_STATUS" == "STALE" ]]; then
    MSG="[HEARTBEAT STALE${HEARTBEAT_AGE_MSG}] — PostToolUse guard registered, no tool calls in >600s. Schema v1.1 active. op=${OP_ID}${ACTIVE_FAILURES}"
elif [[ "$ENFORCEMENT_STATUS" == "NEVER_FIRED" ]]; then
    MSG="[RETRY_SCHEMA v1.1] enforcement registered but not yet fired this session${HEARTBEAT_AGE_MSG}. op=${OP_ID}. Hard cap: 3 attempts. On stop: switch_tool→switch_protocol→operator_eyes→operator_hands.${ACTIVE_FAILURES}"
else
    MSG="[RETRY_SCHEMA v1.1 OK] op=${OP_ID} | hard_cap=3 | on_stop: switch_tool→switch_protocol→operator_eyes→operator_hands${ACTIVE_FAILURES}"
fi

# Escape for JSON string
MSG="${MSG//\\/\\\\}"
MSG="${MSG//\"/\\\"}"

# Build final message — startup non-negotiables prepended only on first prompt
if [[ -n "$SESSION_STARTUP_NON_NEG" ]]; then
    SESSION_STARTUP_NON_NEG="${SESSION_STARTUP_NON_NEG//\\/\\\\}"
    SESSION_STARTUP_NON_NEG="${SESSION_STARTUP_NON_NEG//\"/\\\"}"
    FULL_MSG="${SESSION_STARTUP_NON_NEG} | ${MSG}"
else
    FULL_MSG="${MSG}"
fi

# Output injection — Claude Code UserPromptSubmit hook format
printf '{"hookSpecificReturn":{"additionalSystemPrompt":"%s"}}\n' "$FULL_MSG"

exit 0
