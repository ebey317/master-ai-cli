#!/usr/bin/env bash
# notify_done.sh — Notification hook. Speaks key Claude Code notifications
# via ~/scripts/speak.sh.
#
# Claude Code passes notification JSON on stdin.
# Returns JSON with terminalSequence for desktop notification.

set -euo pipefail

SPEAK="$HOME/scripts/speak.sh"
INPUT="$(cat)"

# Extract message fields
MSG=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    msg = d.get('message') or d.get('title') or ''
    # Skip long/noisy messages
    if len(msg) > 180:
        msg = msg[:177] + '...'
    print(msg.strip())
except:
    pass
" 2>/dev/null || echo "")

# Speak if message is non-empty and speak.sh exists
if [[ -n "$MSG" ]] && [[ -x "$SPEAK" ]]; then
    "$SPEAK" "$MSG" &>/dev/null &
fi

# Return terminal notification sequence
TITLE="Claude Code"
printf '{"terminalSequence": "\033]777;notify;%s;%s\007"}\n' "$TITLE" "${MSG:-notification}"
exit 0
