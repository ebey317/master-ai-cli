#!/usr/bin/env bash
# log_application.sh — PostToolUse hook. Appends a record to the local
# applications log whenever a Bash tool call mentions job application keywords.
#
# Claude Code passes tool result JSON on stdin.
# Exit 0 always (PostToolUse hooks are not blockable).

set -euo pipefail

LOG_FILE="$HOME/MD/applications_log_local.md"
INPUT="$(cat)"

# Extract tool output / result text
RESULT=$(echo "$INPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
# PostToolUse: look in tool_result or output
out = d.get('tool_result') or d.get('output') or d.get('stdout') or str(d)
print(str(out)[:2000])
" 2>/dev/null || echo "")

# Only act if output mentions application-related keywords
if ! echo "$RESULT" | grep -qiE "applied|submitted|application complete|application sent|indeed|ziprecruiter|greenhouse|lever|workday"; then
    exit 0
fi

# Ensure log file exists with header
if [[ ! -f "$LOG_FILE" ]]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    cat > "$LOG_FILE" <<'HEADER'
# Applications Log — Local Mirror

| Date | Notes |
|------|-------|
HEADER
fi

DATE="$(date '+%Y-%m-%d %H:%M')"
echo "| $DATE | PostToolUse hook: application-related output detected. Check session log. |" >> "$LOG_FILE"

echo "[log_application] appended entry to $LOG_FILE" >&2
exit 0
