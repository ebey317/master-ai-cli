#!/bin/bash
# Launches Master AI in a persistent tmux session with supervisor loop.
# If already running, reattaches.
# If tmux session exists but engine is dead (clean exit), relaunches engine.
# Terminal can close — session stays alive.

SESSION="master-ai"

# Supervisor loop: runs master_ai.py forever.
#   exit 99  — explicit user quit ('x' command). Break out, shell returns.
#   exit 42  — 'kick' soft-restart (1s wait).
#   any other exit (0 included) — treat as crash, relaunch after 3s.
# Rationale: a consumer app must always come back up. Clean exit 0 used to
# break the loop, but any bug that happened to exit 0 would leave the user
# stranded at the shell with no obvious way back in. Now only a deliberate
# 'x' (exit 99) ends the session.
# 2026-09-03: master_ai.py's own process env was confirmed missing
# DISPLAY entirely (checked /proc/$PID/environ live — XDG_SESSION_TYPE=tty,
# no DISPLAY at all), even though DBUS_SESSION_BUS_ADDRESS was present and
# `tmux show-environment -t master-ai DISPLAY` showed :0 — tmux's own
# per-session variable store doesn't automatically propagate into an
# already-running shell's environment, only into new panes/windows tmux
# itself spawns. Root cause of a real bug: _launch_desktop_argv-launched
# GUI apps (open desktop app requests) had no X server to connect to,
# so some failed silently (exit 0, no window, no error — DEVNULL ate any
# message explaining why) depending on how much a given app's toolkit
# tolerated a missing DISPLAY before giving up. Falls back to :0 (this
# machine's actual display) only if truly unset, so it's a no-op on any
# environment that already has DISPLAY correctly set.
SUPERVISOR='cd ~ && export DISPLAY="${DISPLAY:-:0}" && if grep -q "^SENSEI_MOUSE=0" ~/.master_ai_settings 2>/dev/null; then export SENSEI_MOUSE=0; else export SENSEI_MOUSE=1; fi && while true; do python3 ~/scripts/master_ai.py 2>>~/scripts/master.crash.log; EXIT=$?; if [ $EXIT -eq 99 ]; then break; fi; if [ $EXIT -eq 42 ]; then sleep 1; continue; fi; echo "[$(date)] Master AI exited (code=$EXIT) — auto-restarting in 3s..." >> ~/scripts/master.crash.log 2>&1; sleep 3; done; clear'

engine_alive() { pgrep -f "python3.*master_ai.py" >/dev/null 2>&1; }

# ── Defensive: always force pane to match the terminal we're launching from ──
# Without this, a session created yesterday with different dims would persist.
# Customer should never have to manually kill tmux to get a full-screen Sensei.
tmux source-file "$HOME/.tmux.conf" 2>/dev/null || true
tmux set-window-option -g aggressive-resize on 2>/dev/null || true
tmux set-window-option -g window-size latest 2>/dev/null || true
COLS=$(tput cols 2>/dev/null || echo 120)
LINES=$(tput lines 2>/dev/null || echo 40)

if tmux has-session -t "$SESSION" 2>/dev/null; then
    # Resize to match current terminal before attaching
    tmux kill-pane -a -t "$SESSION" 2>/dev/null || true
    tmux resize-window -t "$SESSION" -x "$COLS" -y "$LINES" 2>/dev/null || true
    tmux clear-history -t "$SESSION" 2>/dev/null || true
    if engine_alive; then
        echo "Master AI already running — reattaching..."
    else
        echo "Tmux session alive but engine stopped — relaunching engine..."
        tmux send-keys -t "$SESSION" "$SUPERVISOR" Enter
        sleep 1
    fi
else
    echo "Starting Master AI persistent session..."
    # Create with current terminal dims; aggressive-resize handles later changes
    tmux new-session -d -s "$SESSION" -x "$COLS" -y "$LINES"
    tmux kill-pane -a -t "$SESSION" 2>/dev/null || true
    tmux clear-history -t "$SESSION" 2>/dev/null || true
    tmux send-keys -t "$SESSION" "$SUPERVISOR" Enter
fi

# Keep this session auto-synced with whichever client is active.
tmux set-hook -t "$SESSION" client-attached "resize-window -A" 2>/dev/null || true
tmux set-hook -t "$SESSION" client-resized "resize-window -A" 2>/dev/null || true
tmux set-hook -t "$SESSION" window-resized "resize-window -A" 2>/dev/null || true

# Switch if already inside tmux, otherwise attach
if [ -n "$TMUX" ]; then
    tmux switch-client -t "$SESSION"
else
    tmux attach-session -t "$SESSION"
fi
