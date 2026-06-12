#!/usr/bin/env bash
# claf_status.sh — Claude Code status bar.
# Shows CLAF routing: local/cloud-free vs anthropic quota burn.

INPUT=$(cat)
echo "$INPUT" > /tmp/claude_status_raw.json 2>/dev/null

STATS=$(curl -s --max-time 1 http://localhost:8000/stats 2>/dev/null)

ROUTING=$(echo "$STATS" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print('claf=offline')
    sys.exit(0)

by_tier = d.get('by_tier', {})
total = d.get('totals', {}).get('total_calls', 0)
anthropic = by_tier.get('6', {}).get('calls', 0)
offgrid = total - anthropic
pct = (100 * offgrid // total) if total else 0

t0 = by_tier.get('0', {}).get('calls', 0)
t1 = by_tier.get('1', {}).get('calls', 0)

print(f'offgrid={pct}% | local:{t0} cloud:{t1} anthropic:{anthropic}')
" 2>/dev/null)

META=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    d = {}
m = d.get('model') or {}
model = (m.get('display_name') or m.get('id') or '?') if isinstance(m, dict) else str(m or '?')
e = d.get('effort') or {}
effort = e.get('level', '?') if isinstance(e, dict) else str(e or '?')
print(f'{model} | effort={effort}')
" 2>/dev/null)

LINE="${ROUTING} | ${META}"
if [ -z "$ROUTING" ]; then LINE="claf=offline | ${META}"; fi

echo "$LINE"
echo "$LINE" > /tmp/claude_runtime 2>/dev/null
