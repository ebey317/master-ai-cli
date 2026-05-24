#!/usr/bin/env bash
# check_wake.sh — surface new wake-listener events to Claude Code.
#
# Reads /tmp/bc_wake_log.jsonl, compares against /tmp/bc_wake_cursor
# (line number we last saw). Prints any NEW events to stdout in a
# format the conversation can use. Advances the cursor.
#
# Wired as a Claude Code PostToolUse Bash hook in settings.json — fires
# automatically after every Bash tool call. No polling needed; the cadence
# of my Bash activity is the polling frequency.
#
# Exit codes:
#   0  always (never block a tool call on this)

LOG="/tmp/bc_wake_log.jsonl"
CURSOR="/tmp/bc_wake_cursor"

# No log → nothing to do
[ -f "$LOG" ] || exit 0

current=$(wc -l < "$LOG" 2>/dev/null | tr -d ' ')
[ -z "$current" ] && current=0

# Read last cursor (default 0 if missing/invalid)
last=$(cat "$CURSOR" 2>/dev/null | head -1)
case "$last" in
    ''|*[!0-9]*) last=0 ;;
esac

# If nothing new, just update cursor (in case log shrank) and exit
if [ "$current" -le "$last" ]; then
    echo "$current" > "$CURSOR"
    exit 0
fi

# New events exist — emit them
new=$((current - last))
echo "=== WAKE_EVENTS: $new new since last check ==="
tail -n "$new" "$LOG" | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        e = json.loads(line)
        print(f\"  [{e.get('ts','?')}] app={e.get('app','?')!r} summary={e.get('summary','?')!r} body={e.get('body','')[:80]!r}\")
    except Exception:
        print(f'  (unparseable: {line.strip()})')
"

# Advance cursor
echo "$current" > "$CURSOR"
exit 0
