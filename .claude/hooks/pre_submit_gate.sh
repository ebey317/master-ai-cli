#!/usr/bin/env bash
# pre_submit_gate.sh — Blocks BROWSER_SUBMIT unless operator explicitly approved.
#
# Wired as a PreToolUse hook in ~/.claude/settings.json.
# Claude Code passes tool call data as JSON on stdin.
#
# Exit codes:
#   0 = allow the tool call to proceed
#   2 = BLOCK the tool call (Claude Code aborts it, shows reason)
#
# To approve a submit: export DEZZY_SUBMIT_APPROVED=1
# Or create the flag file:  touch /tmp/dezzy_submit_approved

set -euo pipefail

TOOL_INPUT="$(cat)"  # Read the tool call JSON from stdin

# Check approval flag file (survives env resets)
FLAG_FILE="/tmp/dezzy_submit_approved"

if [[ "${DEZZY_SUBMIT_APPROVED:-}" == "1" ]] || [[ -f "$FLAG_FILE" ]]; then
    # Approved — log it and allow
    echo "[pre_submit_gate] BROWSER_SUBMIT approved — proceeding" >&2
    # Remove the flag file so it's one-time approval
    rm -f "$FLAG_FILE"
    exit 0
fi

# Not approved — BLOCK with a clear message
echo "BLOCKED: BROWSER_SUBMIT requires operator approval." >&2
echo "" >&2
echo "To approve ONE submission, run:" >&2
echo "  touch /tmp/dezzy_submit_approved" >&2
echo "Then resume the session." >&2
echo "" >&2
echo "Tool input was: $(echo "$TOOL_INPUT" | head -c 200)" >&2

# Emit JSON to Claude Code explaining the block
cat <<'EOF'
{
  "decision": "block",
  "reason": "BROWSER_SUBMIT blocked by pre_submit_gate safety hook. Operator must run: touch /tmp/dezzy_submit_approved — then resume the session to approve exactly one submission."
}
EOF

exit 2
