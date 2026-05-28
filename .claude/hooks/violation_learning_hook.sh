#!/usr/bin/env bash
# PostToolUse — violation learning hook v1.0
#
# Detects known rule violations from tool calls, writes a ⚠️ memory file,
# and appends to MEMORY.md so userpromptsubmit_inject.sh picks it up next session.
# NO temp file state. NO blocking. Exit 0 always.

set -uo pipefail

HOOK_INPUT="$(cat 2>/dev/null || true)"

TOOL_NAME=$(printf '%s' "$HOOK_INPUT" | python3 -c '
import json, os, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}
print(payload.get("tool_name") or os.environ.get("CLAUDE_TOOL_NAME", ""))
' 2>/dev/null || printf '%s' "${CLAUDE_TOOL_NAME:-}")

TOOL_INPUT=$(printf '%s' "$HOOK_INPUT" | python3 -c '
import json, os, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}
tool_input = payload.get("tool_input")
if tool_input is None:
    print(os.environ.get("CLAUDE_TOOL_INPUT", ""))
elif isinstance(tool_input, str):
    print(tool_input)
else:
    print(json.dumps(tool_input))
' 2>/dev/null || printf '%s' "${CLAUDE_TOOL_INPUT:-}")

MEMORY_DIR="$HOME/.claude/projects/-home-elijah/memory"
MEMORY_INDEX="$MEMORY_DIR/MEMORY.md"
LOG="$HOME/.claude/violation_learning.log"
TS=$(date '+%Y-%m-%d %H:%M:%S')
DATE=$(date '+%Y-%m-%d')

# ── Helper: write/update a violation memory file ─────────────────────────────
write_violation() {
    local slug="$1"
    local title="$2"
    local rule="$3"
    local why="$4"
    local how="$5"
    local file="$MEMORY_DIR/${slug}.md"

    cat > "$file" <<MEMEOF
---
name: ${slug}
description: ⚠️ VIOLATION CAPTURED ${DATE}: ${title}
metadata:
  type: feedback
---

${rule}

**Why:** ${why}

**How to apply:** ${how}

Last captured: ${TS}
MEMEOF

    # Add index pointer if not already present
    if [[ -f "$MEMORY_INDEX" ]] && ! grep -qF "$slug" "$MEMORY_INDEX" 2>/dev/null; then
        echo "- [⚠️ ${title}](${slug}.md) — violation captured ${DATE}" >> "$MEMORY_INDEX"
    fi

    echo "$TS [VIOLATION] $slug: $title" >> "$LOG" 2>/dev/null || true
}

# ── Extract command/url from JSON input ───────────────────────────────────────
get_field() {
    local field="$1"
    printf '%s' "$TOOL_INPUT" | python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    print(d.get('$field', ''))
except Exception:
    print(raw.strip() if '$field' == 'command' else '')
" 2>/dev/null || echo ""
}

# ── Pattern 1: Bare Chrome tab via Bash ───────────────────────────────────────
# Violation: google-chrome <url> creates a tab outside the MCP group (§1a)
if [[ "$TOOL_NAME" == "Bash" ]]; then
    CMD=$(get_field "command")
    if echo "$CMD" | grep -qE 'google-chrome[[:space:]]+(https?://|--new-window[[:space:]]+https?://)'; then
        write_violation \
            "feedback_violation_bare_chrome_tab" \
            "Bare Chrome tab — Bash google-chrome used" \
            "NEVER call \`google-chrome <url>\` from Bash. That creates a tab outside the MCP group that sensei cannot drive." \
            "Violation caught by learning hook on ${DATE}. Rule §1a: all tabs via mcp__sensei__tab_create only." \
            "When a URL needs to open: mcp__sensei__tab_create → mcp__sensei__browse within that tab. Never shell out to google-chrome with a URL argument."
    fi

    # Sub-pattern: IPTV/stream URL opened in browser via Bash (not MPV)
    if echo "$CMD" | grep -qE '(google-chrome|xdg-open|firefox)[[:space:]].*\.(m3u8|ts|m3u)'; then
        write_violation \
            "feedback_violation_iptv_in_browser" \
            "IPTV stream opened in browser instead of MPV" \
            "Stream/IPTV URLs must ALWAYS launch via MPV — never in a browser. Any .ts/.m3u8/.m3u URL = MPV command." \
            "Violation caught on ${DATE}. Rule: 'open [channel/stream]' = query XTREAM API → nohup mpv <url> &. Never open in browser." \
            "When operator says 'open [any channel/sport/stream]': query XTREAM API first, build MPV command, launch via Bash. Never tab_create for a stream URL."
    fi
fi

# ── Pattern 2: Streaming URL opened via sensei tab ───────────────────────────
# Violation: IPTV/stream URL navigated in browser tab instead of MPV
if [[ "$TOOL_NAME" == "mcp__sensei__browse" ]] || [[ "$TOOL_NAME" == "mcp__sensei__tab_create" ]]; then
    URL=$(get_field "url")
    if echo "$URL" | grep -qE '\.(m3u8|ts|m3u)([?#]|$)|plugtv\.xyz/live|xtream'; then
        write_violation \
            "feedback_violation_iptv_in_browser" \
            "IPTV stream opened in browser instead of MPV" \
            "Stream/IPTV URLs must ALWAYS launch via MPV — never in a browser tab. Any .ts/.m3u8/.m3u URL = MPV command." \
            "Violation caught on ${DATE}. Rule: 'open [channel/stream]' = query XTREAM API → nohup mpv <url> &." \
            "When operator says 'open [any channel/sport/stream]': query XTREAM API first, build MPV command, launch via Bash. Never open a sensei tab for a stream URL."
    fi
fi

# ── Pattern 3: sensei__browse audit (track all browse calls for review) ───────
if [[ "$TOOL_NAME" == "mcp__sensei__browse" ]]; then
    URL=$(get_field "url")
    echo "$TS [BROWSE_AUDIT] url=${URL}" >> "$LOG" 2>/dev/null || true
fi

exit 0
