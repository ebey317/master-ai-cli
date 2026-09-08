#!/bin/bash
# Headless autostart — ensures Master AI tmux session exists without attaching.
# Safe to call from .desktop autostart or systemd on any DE/window manager.
# Does NOT open a terminal window. User can `tmux attach -t master-ai` later.

# Frameworks can name the tmux session automatically:
#   1. export SENSEI_SESSION_NAME=master-ai-framework  (highest priority)
#   2. write the desired name to ~/.master_ai_session_name
# Falls back to "master-ai".
SESSION="${SENSEI_SESSION_NAME:-$(cat ~/.master_ai_session_name 2>/dev/null || echo master-ai)}"
SUPERVISOR='cd ~ && while true; do python3 ~/scripts/master_ai.py; EXIT=$?; if [ $EXIT -eq 0 ]; then echo "Master AI exited cleanly."; break; fi; if [ $EXIT -eq 42 ]; then echo "kick requested — restarting..."; sleep 1; continue; fi; echo "[$(date)] Master AI crashed (exit=$EXIT) — restarting in 3s..." | tee -a ~/scripts/master.crash.log; sleep 3; done'

# Use current client dimensions instead of a hardcoded size so the TUI auto-fits
# any terminal (small phone, big monitor, resized window).
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    # Start with no forced geometry; fit to first client that attaches.
    tmux new-session -d -s "$SESSION"

    # Auto-resize the window to whatever client is attached.
    for hook in client-attached client-resized window-resized; do
        tmux set-hook -t "$SESSION" "$hook" "resize-window -A" 2>/dev/null || true
    done

    # Fit to an already-attached client, else fall back to 80x24.
    dims=$(tmux list-clients -t "$SESSION" -F '#{client_width}x#{client_height}' 2>/dev/null | head -1)
    if [ -n "$dims" ]; then
        w=${dims%x*}; h=${dims#*x}
        tmux resize-window -t "$SESSION" -x "$w" -y "$h" 2>/dev/null || true
    else
        tmux resize-window -t "$SESSION" -x 80 -y 24 2>/dev/null || true
    fi

    tmux send-keys -t "$SESSION" "$SUPERVISOR" Enter
fi

# If a client is attached now, force a refit so resized terminals pick up the change.
tmux resize-window -t "$SESSION" -A 2>/dev/null || true

# If Master AI is not currently running inside the session, start it.
if ! tmux list-panes -t "$SESSION" -F '#{pane_current_command}' 2>/dev/null | grep -q master_ai.py; then
    tmux send-keys -t "$SESSION" C-c C-u
    tmux send-keys -t "$SESSION" "$SUPERVISOR" Enter
fi
