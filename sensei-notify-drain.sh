#!/bin/bash
# sensei-notify-drain.sh — deliver notifications spooled by sensei-notify.sh.
# Runs on the host (systemd --user), OUTSIDE the Sensei RUN sandbox, so its uid
# matches the D-Bus socket's peer credentials and EXTERNAL auth succeeds.
export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

SPOOL="${SENSEI_NOTIFY_SPOOL:-$HOME/.master_ai_notify_queue}"
LOCK="$SPOOL.lock"
[[ -s "$SPOOL" ]] || exit 0

exec 9>"$LOCK"
flock -n 9 || exit 0

WORK="$SPOOL.draining"
mv "$SPOOL" "$WORK" 2>/dev/null || exit 0

while IFS=$'\t' read -r urgency title body; do
    [[ -z "$title$body" ]] && continue
    notify-send -u "${urgency:-normal}" "${title:-Sensei}" "$body" || true
done < "$WORK"
rm -f "$WORK"
