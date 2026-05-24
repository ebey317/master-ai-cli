#!/usr/bin/env bash
# claf_status.sh — Claude Code status bar.
# Boxes: claude=<model> | style=<style> | effort=<level>
# Schema confirmed from /tmp/claude_status_raw.json 2026-05-24.

INPUT=$(cat)
echo "$INPUT" > /tmp/claude_status_raw.json 2>/dev/null

LINE=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    d = {}

m = d.get('model') or {}
model = (m.get('display_name') or m.get('id') or '?') if isinstance(m, dict) else str(m or '?')

s = d.get('output_style') or {}
style = (s.get('name') or 'default') if isinstance(s, dict) else 'default'

e = d.get('effort') or {}
effort = e.get('level', '?') if isinstance(e, dict) else str(e or '?')

print(f'claude={model} | style={style} | effort={effort}')
" 2>/dev/null)

if [ -z "$LINE" ]; then LINE="claude=? | style=? | effort=?"; fi

echo "$LINE"
echo "$LINE" > /tmp/claude_runtime 2>/dev/null
