#!/usr/bin/env bash
# rustdesk_audio_off.sh — reverse the remote-audio routing set up by
# rustdesk_audio_on.sh. Restores audio to a local physical sink so the
# operator hears it through speakers / Bluetooth headphones again.
#
# Steps:
#   1. Find a non-null physical sink to restore default to.
#      Preference order: bluez (Bluetooth), then alsa (analog), then any.
#   2. Set that sink as default.
#   3. Unload module-null-sink for "remote_audio".
#
# Idempotent — safe to re-run.

set -u

GREEN=$'\033[1;32m'
RED=$'\033[1;31m'
DIM=$'\033[2m'
RESET=$'\033[0m'

ok()   { printf "${GREEN}OK${RESET}    %s\n" "$1"; }
bad()  { printf "${RED}FAIL${RESET}  %s\n" "$1"; }
info() { printf "${DIM}      %s${RESET}\n" "$1"; }

echo "== rustdesk_audio_off =="

# 1. Find a real physical sink (prefer bluez, then alsa, then anything non-null)
target_sink=$(pactl list short sinks 2>/dev/null \
  | awk '{print $2}' \
  | grep -v '^remote_audio$' \
  | { grep -m1 '^bluez_' || grep -m1 '^alsa_output' || head -1; })

if [[ -z "$target_sink" ]]; then
  bad "no non-null physical sink found — cannot restore default"
  exit 1
fi

# 2. Set as default sink
if pactl set-default-sink "$target_sink" >/dev/null 2>&1; then
  ok "default sink set to $target_sink"
else
  bad "could not set default sink to $target_sink"
fi

# 3. Unload module-null-sink for remote_audio
module_id=$(pactl list short modules 2>/dev/null \
  | awk '/module-null-sink/ && /sink_name=remote_audio/{print $1; exit}')

if [[ -z "$module_id" ]]; then
  info "no 'remote_audio' null sink loaded (already off)"
else
  if pactl unload-module "$module_id" >/dev/null 2>&1; then
    ok "unloaded null sink 'remote_audio' (module #$module_id)"
  else
    bad "could not unload module #$module_id"
  fi
fi
