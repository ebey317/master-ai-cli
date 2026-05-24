#!/bin/bash
# Soft refresh: kills Master AI engine so supervisor loop restarts it.
# Use when: stuck/slow but tmux session is healthy.
# If you need a full rebuild (tmux session broken), use master_ai_kick.sh instead.

SESSION="master-ai"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "No tmux session — running launcher to create one..."
    bash ~/scripts/launch_master_ai.sh
    exit 0
fi

if ! pgrep -f "python3.*sensei_bridge.py" >/dev/null 2>&1; then
    echo "Engine not running — launcher will relaunch it..."
    bash ~/scripts/launch_master_ai.sh
    exit 0
fi

OLD_PIDS=$(pgrep -f "python3.*sensei_bridge.py")
echo "Refreshing Master AI engine (supervisor loop will respawn in ~3s)..."
pkill -TERM -f "python3.*sensei_bridge.py"

# Wait for new process to appear (different PID)
for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    NEW_PIDS=$(pgrep -f "python3.*sensei_bridge.py")
    if [ -n "$NEW_PIDS" ] && [ "$NEW_PIDS" != "$OLD_PIDS" ]; then
        echo "✅ Engine refreshed (took ${i}s)."
        exit 0
    fi
done

echo "⚠ Engine did not respawn within 10s — check ~/scripts/master.crash.log"
exit 1
