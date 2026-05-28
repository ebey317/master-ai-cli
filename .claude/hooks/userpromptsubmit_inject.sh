#!/usr/bin/env bash
# userpromptsubmit_inject.sh — Schema v1.2, Layer 0 Channel B
# Registered as: UserPromptSubmit hook in ~/.claude/settings.json
#
# On FIRST prompt of each session:
#   1. Fires desktop notification + audio cue
#   2. Injects NON-NEGOTIABLES block
#   3. AUTO-LOADS full content of all ⚠️ and ⚡ flagged memories from MEMORY.md
#      → This is what stops the operator from re-explaining confirmed patterns every session.
#
# On every subsequent prompt:
#   4. Injects retry schema status + active failure counters
#
# Exit 0 always (informational only — never blocks the user's message).
# Output format: JSON matching Claude Code's UserPromptSubmit hook spec.
set -euo pipefail

readonly HC="$HOME/.claude"
readonly HEALTH="$HC/.hook_health.json"
readonly STATE="$HC/.retry_state.json"
readonly KILL_SWITCH="$HC/.retry_kill_switch"
readonly LOG="$HC/retry_policy.log"
readonly MEMORY_DIR="$HC/projects/-home-elijah/memory"
readonly MEMORY_INDEX="$MEMORY_DIR/MEMORY.md"

TS=$(date '+%Y-%m-%d %H:%M:%S')

# ── SESSION START NOTIFICATION ────────────────────────────────────────────────
HOOK_INPUT="$(cat 2>/dev/null || true)"
SESSION_ID="$(printf '%s' "$HOOK_INPUT" | jq -r '.session_id // ""' 2>/dev/null || true)"
SESSION_STARTUP_NON_NEG=""

if [[ -n "$SESSION_ID" ]]; then
    SESSION_FLAG="/tmp/claude_session_notified_${SESSION_ID}"
    if [[ ! -f "$SESSION_FLAG" ]]; then
        touch "$SESSION_FLAG"

        # Visual desktop notification
        command -v notify-send &>/dev/null && \
            DISPLAY="${DISPLAY:-:0}" notify-send \
                "🤖 Claude Online" \
                "Session ready. $(date '+%-I:%M %p')" \
                --urgency=normal \
                --expire-time=5000 \
                2>/dev/null & disown

        # Audio cue
        [[ -x "$HOME/scripts/speak.sh" ]] && \
            "$HOME/scripts/speak.sh" "Claude is online." &>/dev/null & disown

        # Log session start
        echo "$TS [SESSION_START] id=${SESSION_ID}" >> "$LOG" 2>/dev/null || true

        # ── NON-NEGOTIABLES ────────────────────────────────────────────────
        SESSION_STARTUP_NON_NEG="[NON-NEGOTIABLES v1.2] "
        SESSION_STARTUP_NON_NEG+="#1 MCP TABS ONLY: No bare Chrome tabs. No google-chrome <url>. tab_create via sensei only. "
        SESSION_STARTUP_NON_NEG+="#2 NO EXTENSION DEPENDENCY: Full browser parity via Playwright/CDP without extension. "
        SESSION_STARTUP_NON_NEG+="#3 OPERATOR SEES AUTHENTICATED ACTIONS: Any tool using operator login MUST be operator-visible. Narrate before. Open URL in visible tab BEFORE state-changing calls. "
        SESSION_STARTUP_NON_NEG+="#4 RETRY HARD CAP: 3 attempts per (operation_id, tool). switch_tool→switch_protocol→operator_eyes→operator_hands. OBSERVABILITY_FAILURE+PERMANENT+UNKNOWN+REFLECTION_FAILURE = zero retries. "
        SESSION_STARTUP_NON_NEG+="#5 BROWSER CLICK METHOD: read_full → CSS selector string → one click at a time → screenshot. ONLY reliable method. Batch/ref clicks fail silently. "
        SESSION_STARTUP_NON_NEG+="#6 BROWSER_SUBMIT ESCALATION: When click/double_click/batch/js_eval all fail (isTrusted-blocked buttons, gov sites): curl POST to http://127.0.0.1:8080/extension/queue with kind=BROWSER_SUBMIT. Confirmed on Indiana Uplink ID.me 2026-05-27. "
        SESSION_STARTUP_NON_NEG+="#7 CHROME DEV TOOLS LIVE: mcp__sensei__console_logs + mcp__sensei__network_requests are wired and confirmed working. Use them for observability — don't guess what the page is doing. "
        SESSION_STARTUP_NON_NEG+="#8 TAB OPEN = tab_create to google.com. That is the ONLY tab-open method. No alternatives. "

        # ── AUTO-LOAD ⚠️ and ⚡ FLAGGED MEMORIES ──────────────────────────
        # Parse MEMORY.md, find flagged lines, read full file body, inject.
        # This is what stops the operator re-explaining confirmed patterns every session.
        if [[ -f "$MEMORY_INDEX" ]]; then
            MEMORY_BLOCK="[CONFIRMED PATTERNS — auto-loaded from flagged memories]: "
            MEM_COUNT=0
            while IFS= read -r line; do
                # Only process lines with ⚠️ or ⚡
                if [[ "$line" == *"⚠️"* ]] || [[ "$line" == *"⚡"* ]]; then
                    # Extract filename from markdown link syntax: [Title](filename.md)
                    fname=$(echo "$line" | sed -n 's/.*(\([^)]*\.md\)).*/\1/p')
                    if [[ -n "$fname" ]] && [[ -f "$MEMORY_DIR/$fname" ]]; then
                        # Strip frontmatter (content after second --- line)
                        body=$(awk 'BEGIN{n=0} /^---/{n++; next} n>=2{print}' \
                              "$MEMORY_DIR/$fname" 2>/dev/null \
                              | grep -v '^#' \
                              | tr '\n' ' ' \
                              | tr -s ' ' \
                              | sed 's/^ //; s/ $//')
                        # Truncate to 250 chars
                        body="${body:0:250}"
                        if [[ -n "$body" ]]; then
                            # Extract title
                            title=$(echo "$line" | sed -n 's/.*\[\([^]]*\)\].*/\1/p' | head -1)
                            title="${title//⚠️ /}"
                            title="${title//⚡ /}"
                            MEMORY_BLOCK+="<<${title}>> ${body} | "
                            MEM_COUNT=$(( MEM_COUNT + 1 ))
                        fi
                    fi
                fi
            done < "$MEMORY_INDEX"

            if [[ $MEM_COUNT -gt 0 ]]; then
                SESSION_STARTUP_NON_NEG+=" ${MEMORY_BLOCK}"
                echo "$TS [SESSION_START] injected ${MEM_COUNT} flagged memories" >> "$LOG" 2>/dev/null || true
            fi
        fi
        # ──────────────────────────────────────────────────────────────────

        # ── TOPOLOGY AGENT — live system briefing ─────────────────────────
        TOPO_SCRIPT="$HOME/scripts/topology_agent.py"
        if [[ -x "$TOPO_SCRIPT" ]] || [[ -f "$TOPO_SCRIPT" ]]; then
            TOPO_OUT=$(python3 "$TOPO_SCRIPT" --short 2>/dev/null || true)
            if [[ -n "$TOPO_OUT" ]]; then
                SESSION_STARTUP_NON_NEG+=" ${TOPO_OUT}"
                echo "$TS [SESSION_START] topology_agent injected" >> "$LOG" 2>/dev/null || true
            fi
        fi
        # ──────────────────────────────────────────────────────────────────
    fi
fi

# ── Kill switch ───────────────────────────────────────────────────────────────
if [[ -f "$KILL_SWITCH" ]]; then
    printf '{"hookSpecificReturn":{"additionalSystemPrompt":"[RETRY_SCHEMA] kill_switch=ACTIVE. Enforcement suspended."}}\n'
    exit 0
fi

# Require jq
if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

# ── Heartbeat freshness ───────────────────────────────────────────────────────
ENFORCEMENT_STATUS="OK"
HEARTBEAT_AGE_MSG=""
if [[ -f "$HEALTH" ]]; then
    LAST_BEAT=$(jq -r '.last_heartbeat // ""' "$HEALTH" 2>/dev/null || echo "")
    if [[ -n "$LAST_BEAT" ]]; then
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

# ── Active failure counters ───────────────────────────────────────────────────
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

# ── Build retry schema status line ───────────────────────────────────────────
if [[ "$ENFORCEMENT_STATUS" == "STALE" ]]; then
    MSG="[HEARTBEAT STALE${HEARTBEAT_AGE_MSG}] — PostToolUse guard registered, no tool calls in >600s. Schema v1.2 active. op=${OP_ID}${ACTIVE_FAILURES}"
elif [[ "$ENFORCEMENT_STATUS" == "NEVER_FIRED" ]]; then
    MSG="[RETRY_SCHEMA v1.2] enforcement registered but not yet fired this session${HEARTBEAT_AGE_MSG}. op=${OP_ID}. Hard cap: 3 attempts. On stop: switch_tool→switch_protocol→operator_eyes→operator_hands.${ACTIVE_FAILURES}"
else
    MSG="[RETRY_SCHEMA v1.2 OK] op=${OP_ID} | hard_cap=3 | on_stop: switch_tool→switch_protocol→operator_eyes→operator_hands${ACTIVE_FAILURES}"
fi

# ── JSON escape ───────────────────────────────────────────────────────────────
MSG="${MSG//\\/\\\\}"
MSG="${MSG//\"/\\\"}"

if [[ -n "$SESSION_STARTUP_NON_NEG" ]]; then
    SESSION_STARTUP_NON_NEG="${SESSION_STARTUP_NON_NEG//\\/\\\\}"
    SESSION_STARTUP_NON_NEG="${SESSION_STARTUP_NON_NEG//\"/\\\"}"
    FULL_MSG="${SESSION_STARTUP_NON_NEG} | ${MSG}"
else
    FULL_MSG="${MSG}"
fi

printf '{"hookSpecificReturn":{"additionalSystemPrompt":"%s"}}\n' "$FULL_MSG"

exit 0
