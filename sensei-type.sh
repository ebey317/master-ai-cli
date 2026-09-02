#!/bin/bash
# sensei-type.sh — type text into the live Sensei CLI prompt, no clipboard needed.
# Usage: sensei-type.sh "text to type"          # types it, waits for you to hit Enter
#        sensei-type.sh --enter "text"          # types it AND submits
#
# Why: Master AI reads input with plain readline/input(), so a pasted multi-line
# block submits one turn PER LINE. This flattens newlines to spaces so any block
# arrives as a single prompt. Nothing touches the X clipboard, so it works with
# no mouse and no keyboard.
TARGET="${SENSEI_PANE:-master-ai:0.0}"
SUBMIT=0
[[ "$1" == "--enter" ]] && { SUBMIT=1; shift; }
TEXT="$*"
[[ -z "$TEXT" ]] && { echo "usage: sensei-type.sh [--enter] \"text\"" >&2; exit 2; }
tmux has-session -t "${TARGET%%:*}" 2>/dev/null || { echo "no tmux session ${TARGET%%:*}" >&2; exit 1; }
FLAT=$(printf '%s' "$TEXT" | tr '\n' ' ')
tmux send-keys -t "$TARGET" -l -- "$FLAT"
(( SUBMIT )) && tmux send-keys -t "$TARGET" Enter
echo "typed ${#FLAT} chars into $TARGET$( ((SUBMIT)) && echo ' + Enter')"
