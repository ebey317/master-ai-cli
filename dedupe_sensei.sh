#!/usr/bin/env bash
# dedupe_sensei.sh — kill the orphan sensei_mcp_server, keep the active one.
# Identifies orphan by parent process age: older parent = stale session.
# Safe: SIGTERM first, SIGKILL only if still alive after 3s.

set -euo pipefail

echo "=== dedupe_sensei.sh ==="
echo ""

# Find all sensei_mcp_server PIDs
PIDS=($(pgrep -f sensei_mcp_server.py 2>/dev/null || true))

if [ "${#PIDS[@]}" -eq 0 ]; then
    echo "No sensei_mcp_server processes found. Nothing to do."
    exit 0
fi

if [ "${#PIDS[@]}" -eq 1 ]; then
    echo "Only one sensei_mcp_server running (PID ${PIDS[0]}). Nothing to deduplicate."
    ps -o pid,ppid,etime,args -p "${PIDS[0]}"
    exit 0
fi

echo "Found ${#PIDS[@]} sensei_mcp_server processes:"
ps -o pid,ppid,etime,args -p "${PIDS[@]}"
echo ""

# Determine which PID has the oldest parent claude process (= orphan from a dead session).
# Strategy: for each PID, get parent PID, then get that parent's elapsed seconds.
# The one whose parent started LONGEST ago is the orphan.

oldest_parent_secs=0
orphan_pid=""
keeper_pid=""

for pid in "${PIDS[@]}"; do
    ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || echo "0")
    if [ -z "$ppid" ] || [ "$ppid" = "0" ]; then
        continue
    fi
    # Get elapsed time of parent in seconds
    etime=$(ps -o etimes= -p "$ppid" 2>/dev/null | tr -d ' ' || echo "0")
    etime="${etime:-0}"
    echo "  PID $pid → parent $ppid elapsed ${etime}s"
    if [ "$etime" -gt "$oldest_parent_secs" ]; then
        oldest_parent_secs="$etime"
        orphan_pid="$pid"
    fi
done

# The keeper is whichever PID isn't the orphan
for pid in "${PIDS[@]}"; do
    if [ "$pid" != "$orphan_pid" ]; then
        keeper_pid="$pid"
        break
    fi
done

echo ""
if [ -z "$orphan_pid" ]; then
    echo "Could not determine orphan. No action taken."
    exit 1
fi

echo "Orphan  → PID $orphan_pid  (parent started ${oldest_parent_secs}s ago — oldest)"
echo "Keeper  → PID $keeper_pid"
echo ""

# SIGTERM the orphan
echo "Sending SIGTERM to PID $orphan_pid …"
kill -TERM "$orphan_pid" 2>/dev/null || true

# Wait up to 3s for it to die
for i in 1 2 3; do
    sleep 1
    if ! kill -0 "$orphan_pid" 2>/dev/null; then
        echo "  PID $orphan_pid exited cleanly."
        break
    fi
    if [ "$i" -eq 3 ]; then
        echo "  PID $orphan_pid still alive after 3s — sending SIGKILL"
        kill -KILL "$orphan_pid" 2>/dev/null || true
        sleep 0.5
    fi
done

# Confirm
echo ""
echo "--- process check after dedup ---"
REMAINING=($(pgrep -f sensei_mcp_server.py 2>/dev/null || true))
if [ "${#REMAINING[@]}" -eq 0 ]; then
    echo "WARNING: no sensei_mcp_server running. Relaunch with: python3 ~/scripts/sensei_mcp_server.py"
elif [ "${#REMAINING[@]}" -eq 1 ]; then
    echo "OK — one sensei_mcp_server running:"
    ps -o pid,ppid,etime,args -p "${REMAINING[0]}"
else
    echo "Still ${#REMAINING[@]} processes running — manual inspection needed:"
    ps -o pid,ppid,etime,args -p "${REMAINING[@]}"
fi

echo ""
echo "--- claude mcp list | grep sensei ---"
claude mcp list 2>/dev/null | grep -i sensei || echo "(could not run claude mcp list)"
