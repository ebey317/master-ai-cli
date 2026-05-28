#!/usr/bin/env bash
# Enforces NON-NEGOTIABLE §1a: every new URL must open in an MCP tab.
# Blocks mcp__sensei__browse if it's being used to navigate a bare/existing tab
# instead of a tab created via mcp__sensei__tab_create.
#
# Hook type: PreToolUse
# Matcher: mcp__sensei__browse

set -euo pipefail

HOOK_INPUT="$(cat 2>/dev/null || true)"
TOOL_NAME=$(printf '%s' "$HOOK_INPUT" | python3 -c '
import json, os, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}
print(payload.get("tool_name") or os.environ.get("CLAUDE_TOOL_NAME", ""))
' 2>/dev/null || printf '%s' "${CLAUDE_TOOL_NAME:-}")

# Only enforce on sensei browse
if [[ "$TOOL_NAME" != "mcp__sensei__browse" ]]; then
  exit 0
fi

# Check if a tab_create was the immediately preceding sensei call.
# We do this by reading the last_sensei_action temp file written by PostToolUse.
LAST_ACTION_FILE="/tmp/.last_sensei_action"

if [[ ! -f "$LAST_ACTION_FILE" ]]; then
  echo "MCP_TAB_GUARD: BLOCKED — sensei__browse called without a prior tab_create. Use mcp__sensei__tab_create first. (§1a)" >&2
  exit 1
fi

LAST_ACTION=$(cat "$LAST_ACTION_FILE")

if [[ "$LAST_ACTION" != "tab_create" ]]; then
  echo "MCP_TAB_GUARD: BLOCKED — sensei__browse must be preceded by sensei__tab_create. Last action was: $LAST_ACTION. §1a violation." >&2
  exit 1
fi

# Clear the flag so it can't be reused
rm -f "$LAST_ACTION_FILE"
exit 0
