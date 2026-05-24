#!/usr/bin/env bash
# Start the workflow with browser-Claude (BC to Claude Code, EC to Elijah —
# same entity, two names).
#
# What this brings up / verifies in one shot:
#   1. BC-wake listener (systemd --user unit bc-wake-listener.service) is
#      enabled + running. Listener writes /tmp/bc_reply_ready + JSONL log.
#   2. Required tools on PATH: dbus-monitor, scrot, xdotool, xclip.
#   3. Verified panel paste-target coordinates.
#   4. workflow_cadence.py is present and importable.
#   5. PostToolUse(Bash) + SessionStart hooks present in
#      ~/.claude/settings.json — counter ticks on git commit, every 5th
#      auto-fires snapshot + push + memory reminder.
#   6. Current commit counter.
#   7. ~/Desktop/AI_CONTEXT/ exists and is writable.
#
# Then prints two quick-reference cards: dialogue loop and commit cadence.
#
# Aliases: start_bc_workflow.sh and start_ec_workflow.sh symlink to this.

set -u

GREEN=$'\033[1;32m'
RED=$'\033[1;31m'
DIM=$'\033[2m'
RESET=$'\033[0m'

ok()   { printf "${GREEN}OK${RESET}    %s\n" "$1"; }
bad()  { printf "${RED}FAIL${RESET}  %s\n" "$1"; }
info() { printf "${DIM}      %s${RESET}\n" "$1"; }

echo "== browser-Claude workflow (BC = EC, same entity) =="
echo

# --- BC-wake listener side -------------------------------------------------
echo "-- listener / paste channel --"
unit=bc-wake-listener.service
if systemctl --user is-enabled --quiet "$unit"; then
  ok "listener unit enabled"
else
  bad "listener unit NOT enabled — run: systemctl --user enable --now $unit"
fi
if systemctl --user is-active --quiet "$unit"; then
  ok "listener unit running"
else
  bad "listener unit NOT running — run: systemctl --user start $unit"
fi

if [[ -f /tmp/bc_reply_ready ]]; then
  ok "sentinel /tmp/bc_reply_ready (last: $(stat -c %y /tmp/bc_reply_ready))"
else
  info "sentinel /tmp/bc_reply_ready not yet created (first BC notification creates it)"
fi
if [[ -f /tmp/bc_wake_log.jsonl ]]; then
  n=$(wc -l < /tmp/bc_wake_log.jsonl)
  ok "log /tmp/bc_wake_log.jsonl ($n entries)"
else
  info "log /tmp/bc_wake_log.jsonl not yet created"
fi

for cmd in dbus-monitor scrot xdotool xclip; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "tool: $cmd"
  else
    bad "tool MISSING: $cmd"
  fi
done

echo
echo "  Verified BC panel paste-target (1280x800 Madam-Mary, side panel right):"
echo "    xdotool mousemove 566 655 && xdotool click 1 && xdotool key ctrl+v && xdotool key Return"
echo "  Re-discover with: scrot -o /tmp/panel_now.png  then read pixel coords"

# --- EC commit-cadence side ------------------------------------------------
echo
echo "-- commit cadence (Claude Code side) --"

if [[ -x /home/elijah/scripts/workflow_cadence.py ]]; then
  ok "cadence script: ~/scripts/workflow_cadence.py"
else
  bad "cadence script missing or not executable"
fi

settings=~/.claude/settings.json
if [[ -f "$settings" ]]; then
  if python3 -c "
import json,sys
d=json.load(open('$settings'))
h=d.get('hooks',{})
post=any('workflow_cadence' in (x.get('command','')) for grp in h.get('PostToolUse',[]) for x in grp.get('hooks',[]) or [])
start=any('workflow_cadence' in (x.get('command','')) for grp in h.get('SessionStart',[]) for x in grp.get('hooks',[]) or [])
sys.exit(0 if (post and start) else 1)
" 2>/dev/null; then
    ok "hooks installed (PostToolUse + SessionStart)"
  else
    bad "hooks NOT installed — run: python3 ~/scripts/add_workflow_hook.py"
  fi
else
  bad "$settings missing"
fi

status=$(python3 /home/elijah/scripts/workflow_cadence.py status 2>/dev/null || echo "status failed")
ok "counter: $status"

if [[ -d ~/Desktop/AI_CONTEXT && -w ~/Desktop/AI_CONTEXT ]]; then
  ok "snapshot dir ~/Desktop/AI_CONTEXT (writable)"
else
  bad "snapshot dir ~/Desktop/AI_CONTEXT missing or not writable"
fi

# --- Audio routing — now explicit, not auto -------------------------------
# Audio routing was previously inlined here and fired automatically on every
# workflow start (silencing local speakers via the false-firing RustDesk
# capture gate — 2026-05-19 incident). Now split into explicit operator-
# invoked commands:
#   ~/scripts/rustdesk_audio_on.sh   — route audio through null sink for remote
#   ~/scripts/rustdesk_audio_off.sh  — restore audio to local physical sink
echo
echo "-- audio routing --"
info "no auto-routing on workflow start (see ~/scripts/rustdesk_audio_on.sh / _off.sh)"

# --- Cards -----------------------------------------------------------------
echo
cat <<'EOF'
== Dialogue loop ==
  1. Wait for WAKE event (in-chat notification when listener fires)
  2. scrot -o /tmp/panel_now.png ; Read /tmp/panel_now.png
     Anchor at bottom of panel, scroll up to start of his latest reply,
     read top-to-bottom. `build it` at the bottom exits brainstorm.
  3. Compose response. No PII, details not numbers.
  4. cat > /tmp/prompt.txt <<EOT
     ...message...
     EOT
     xclip -selection clipboard < /tmp/prompt.txt
     xdotool mousemove 566 655 && xdotool click 1 && xdotool key ctrl+v && xdotool key Return
  5. scrot -o /tmp/panel_now.png  to verify the paste landed.

  Listener log: tail -f /tmp/bc_wake_log.jsonl
  Listener journal: journalctl --user -u bc-wake-listener.service -f

== Commit cadence ==
  Per commit (manual)   change + test + commit + tasklist
  Every 5th commit      AI_CONTEXT snapshot + git push + memory reminder (auto)
  On-demand triggers    "ship" / "save state" / "where are we" / "commit and push"

  Status:   python3 ~/scripts/workflow_cadence.py status
  Snapshot: python3 ~/scripts/workflow_cadence.py snapshot
  Push:     python3 ~/scripts/workflow_cadence.py push

== Audio routing (explicit, no auto) ==
  Remote on tablet/phone:  bash ~/scripts/rustdesk_audio_on.sh
  Back to local speakers:  bash ~/scripts/rustdesk_audio_off.sh
EOF
