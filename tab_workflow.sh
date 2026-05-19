#!/usr/bin/env bash
# tab_workflow.sh — start/stop the tab-agnostic pixel-diff wake listener.
#
# Usage:
#   tab_workflow.sh on  [REGION]      # default REGION = 0,0,640,800 (Gemini left panel)
#   tab_workflow.sh off
#   tab_workflow.sh status
#
# When `on` is invoked, the script captures the CURRENTLY FOCUSED window's
# ID as the wake-injection target (so run this FROM the Claude Code
# terminal). It then launches tab_wake_listener.py in the background, which
# screenshots REGION every 3s and types `[WAKE]` into the target window
# when content stops changing.

set -u

LISTENER=/home/elijah/scripts/tab_wake_listener.py
PIDFILE=/tmp/tab_wake_listener.pid
WINFILE=/tmp/tab_wake_target_window
LOGFILE=/tmp/tab_wake_listener.out

cmd=${1:-status}

case "$cmd" in
  on)
    region=${2:-0,0,640,800}
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$PIDFILE")) — run 'tab_workflow.sh off' first"
      exit 1
    fi
    wid=$(xdotool getactivewindow 2>/dev/null)
    if [[ -z "$wid" ]]; then
      echo "ERROR: could not get active window id. xdotool installed?"
      exit 2
    fi
    # Convert decimal to hex for readability/logging.
    wid_hex=$(printf '0x%08x' "$wid")
    wname=$(xdotool getwindowname "$wid" 2>/dev/null || echo "(unknown)")
    echo "$wid" > "$WINFILE"
    echo "wake target window: $wid_hex  ($wname)"
    echo "watch region:       $region"
    nohup python3 -u "$LISTENER" \
      --region "$region" \
      --window-id "$wid" \
      > "$LOGFILE" 2>&1 &
    sleep 0.5
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "listener started (pid $(cat "$PIDFILE")), log: $LOGFILE"
    else
      echo "WARN: listener may not have started — check $LOGFILE"
      tail -20 "$LOGFILE" 2>/dev/null
      exit 3
    fi
    ;;

  off)
    if [[ ! -f "$PIDFILE" ]]; then
      echo "not running (no pidfile)"
      exit 0
    fi
    pid=$(cat "$PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid"
      sleep 0.5
      if kill -0 "$pid" 2>/dev/null; then
        echo "did not exit on SIGTERM, sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null
      fi
      echo "stopped (pid $pid)"
    else
      echo "pidfile stale (pid $pid not running)"
    fi
    rm -f "$PIDFILE"
    ;;

  status)
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      pid=$(cat "$PIDFILE")
      wname=""
      if [[ -f "$WINFILE" ]]; then
        wid=$(cat "$WINFILE")
        wname=$(xdotool getwindowname "$wid" 2>/dev/null || echo "(unknown)")
      fi
      echo "RUNNING  pid=$pid  target=$wname"
      echo "log tail:"
      tail -5 "$LOGFILE" 2>/dev/null
      echo "wake log tail:"
      tail -5 /tmp/tab_wake_log.jsonl 2>/dev/null
    else
      echo "NOT RUNNING"
    fi
    ;;

  *)
    echo "Usage: $0 on [REGION] | off | status"
    exit 1
    ;;
esac
