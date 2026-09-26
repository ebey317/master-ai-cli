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
SUPERVISOR='cd ~ && export DISPLAY="${DISPLAY:-:0}" && if grep -q "^SENSEI_MOUSE=0" ~/.master_ai_settings 2>/dev/null; then export SENSEI_MOUSE=0; else export SENSEI_MOUSE=1; fi && while true; do /usr/bin/python3 ~/scripts/master_ai.py 2>>~/scripts/master.crash.log; EXIT=$?; if [ $EXIT -eq 99 ]; then break; fi; if [ $EXIT -eq 42 ]; then sleep 1; continue; fi; echo "[$(date)] Master AI exited (code=$EXIT) — auto-restarting in 3s..." >> ~/scripts/master.crash.log 2>&1; sleep 3; done; clear'

engine_alive() { pgrep -f "python3.*master_ai.py" >/dev/null 2>&1; }

# 2026-09-11: aoe already gives every managed pane its OWN isolated tmux
# session (names like "aoe_<task>_<hash>") — that's how its other agent
# types (claude, hermes) get per-task isolation for free. This script used
# to ignore that and always redirect into one shared global "master-ai"
# session instead, no matter which pane called it. Effect: every aoe task
# assigned to master-ai collapsed onto the same session/process, so a
# single deliberate quit (exit 99) sent from anywhere attached to it killed
# every other aoe task nested onto it too (confirmed: "Lithuanians" and
# "Malay" panes both died at the same instant when the shared session was
# quit, even though neither one had any actual error).
#
# Fix: if we're already running inside an aoe-owned session, just run the
# engine here — no second nested session to share or collide on. Only fall
# back to the persistent, reattachable shared "master-ai" session for the
# human case (e.g. typing `master` directly in a real terminal), which is
# not aoe-named and genuinely wants "same long-lived conversation from
# anywhere."
if [ -n "$TMUX" ]; then
    CURRENT_SESSION=$(tmux display-message -p '#S' 2>/dev/null)
    case "$CURRENT_SESSION" in
        aoe_*)
            exec bash -c "$SUPERVISOR"
            ;;
    esac
fi

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

# 2026-09-11: this used to branch on whether `tmux switch-client` succeeded,
# on the theory that success meant "a human is genuinely attached to THIS
# pane, redirect their view." Wrong: switch-client redirects whatever
# client is attached ANYWHERE on the tmux server, not one scoped to the
# calling pane. Elijah's own real terminal is very often already attached
# to the master-ai session for unrelated reasons, so switch-client kept
# "succeeding" for aoe's managed pane too -- which then just parked in the
# poll loop forever showing nothing, because nothing was ever actually
# attached to THAT pane specifically. Confirmed live: an aoe session
# ("Malay") sat 1h+ with its launcher blocked in `sleep` inside that loop,
# frozen on the very first printed line, while `aoe ps` and the real
# master-ai terminal were both completely healthy.
#
# There is no reliable signal (env var, etc.) to tell "human typed `master`
# inside their own separate tmux session" apart from "aoe (or any other
# orchestrator) is running this as a managed pane's entire command" -- both
# are just a non-interactive `bash launch_master_ai.sh` with $TMUX set. So:
# always attach-session when $TMUX is set, never switch-client. A human
# manually running this from inside their own tmux gets a nested attach
# instead of a clean view-switch (mildly redundant status bars, otherwise
# harmless, detach as usual) -- a small UX cost, and worth it for a
# managed pane to reliably show and keep showing real content instead of
# intermittently freezing depending on unrelated client state elsewhere on
# the same tmux server.
if [ -n "$TMUX" ]; then
    # tmux refuses attach-session outright ("sessions should be nested with
    # care, unset $TMUX to force") whenever $TMUX is already set, regardless
    # of whether anything is actually attached behind it. Unset TMUX for
    # just this call, per tmux's own suggested fix.
    env -u TMUX tmux attach-session -t "$SESSION"
else
    tmux attach-session -t "$SESSION"
fi
