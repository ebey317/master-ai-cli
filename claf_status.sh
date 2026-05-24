#!/usr/bin/env bash
# claf_status.sh — Claude Code status bar.
# Boxes: claude=<model> | style=<style> | effort=<level>
# effort comes from stdin JSON (live) or falls back to settings.json.
# Writes to /tmp/claude_runtime so CLAF watch can mirror it.

INPUT=$(cat)
SETTINGS="$HOME/.claude/settings.json"

read MODEL STYLE EFFORT < <(echo "$INPUT" | python3 -c "
import json, sys

raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    d = {}

# model
m = d.get('model') or {}
model = (m.get('display_name') or m.get('id') or '') if isinstance(m, dict) else str(m or '')

# style
s = d.get('output_style') or {}
style = (s.get('name') or 'default') if isinstance(s, dict) else (str(s) or 'default')

# effort — may live in a few places depending on CC version
effort = (
    d.get('effortLevel')
    or d.get('effort_level')
    or (d.get('model') or {}).get('effort_level') if isinstance(d.get('model'), dict) else None
    or ''
)

print(model or '?', style or 'default', effort or '')
" 2>/dev/null)

# effort fallback: read from settings.json if stdin didn't carry it
if [ -z "$EFFORT" ]; then
    EFFORT=$(python3 -c "
import json
try:
    d = json.load(open('$SETTINGS'))
    print(d.get('effortLevel','?'))
except:
    print('?')
" 2>/dev/null)
fi

[ -z "$MODEL" ] && MODEL="?"
[ -z "$STYLE" ] && STYLE="?"
[ -z "$EFFORT" ] && EFFORT="?"

LINE="claude=${MODEL} | style=${STYLE} | effort=${EFFORT}"
echo "$LINE"
echo "$LINE" > /tmp/claude_runtime 2>/dev/null
