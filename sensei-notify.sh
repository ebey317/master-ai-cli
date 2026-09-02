#!/bin/bash
# sensei-notify.sh — desktop popup notification, callable from Sensei (or anywhere).
# Usage: sensei-notify.sh "title" "body text" [urgency]
#   urgency = low | normal | critical (default: normal)
#
# Two failure modes this handles:
#
# 1. Missing desktop env. A process spawned outside the desktop session (a
#    systemd --user unit that started before the env was exported, a bare SSH
#    shell, tmux inherited from an old server) has no DISPLAY/DBUS in its own
#    environment even though the session is alive. Fixed by the defaults below.
#
# 2. The Sensei RUN sandbox. Every RUN command is wrapped in
#    `unshare -U --map-root-user`, so inside it the process believes it is uid 0.
#    D-Bus EXTERNAL auth has the client assert its uid; the bus checks the real
#    peer credentials (1000) and rejects the mismatch with
#    "Exhausted all available authentication mechanisms (tried: EXTERNAL)".
#    No amount of env fixing helps -- the namespace IS the wall. So on failure we
#    append to a spool file, which sensei-notify-drain.sh (running on the host,
#    outside any namespace) picks up and delivers.
export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

SPOOL="${SENSEI_NOTIFY_SPOOL:-$HOME/.master_ai_notify_queue}"

TITLE="${1:-Sensei}"
BODY="${2:-}"
URGENCY="${3:-normal}"
if [[ "$URGENCY" != "low" && "$URGENCY" != "normal" && "$URGENCY" != "critical" ]]; then
    URGENCY="normal"
fi

# Strip tabs/newlines so one notification is exactly one spool line.
sanitize() { printf '%s' "$1" | tr '\t\n' '  '; }

if out=$(notify-send -u "$URGENCY" "$TITLE" "$BODY" 2>&1); then
    exit 0
fi

printf '%s\t%s\t%s\n' "$URGENCY" "$(sanitize "$TITLE")" "$(sanitize "$BODY")" >> "$SPOOL" 2>/dev/null || {
    echo "notify-send failed and spool unwritable: $out" >&2
    exit 1
}
echo "queued (direct delivery blocked: ${out:-unknown}); drain will deliver"
exit 0
