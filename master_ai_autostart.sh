#!/bin/bash
# Headless autostart — ensures Master AI tmux session exists without attaching.
# Safe to call from .desktop autostart or systemd on any DE/window manager.
# Does NOT open a terminal window. User can `tmux attach -t master-ai` later.

SESSION="master-ai"
SUPERVISOR='cd ~ && while true; do python3 ~/scripts/sensei_bridge.py; EXIT=$?; if [ $EXIT -eq 0 ]; then echo "Master AI exited cleanly."; break; fi; if [ $EXIT -eq 42 ]; then echo "kick requested — restarting..."; sleep 1; continue; fi; echo "[$(date)] Master AI crashed (exit=$EXIT) — restarting in 3s..." | tee -a ~/scripts/master.crash.log; sleep 3; done'

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux new-session -d -s "$SESSION" -x 220 -y 50
    tmux send-keys -t "$SESSION" "$SUPERVISOR" Enter
fi
