#!/usr/bin/env bash
# rustdesk_audio_on.sh — route computer audio through a null sink so a
# remote RustDesk client (phone/tablet) can hear it while local speakers
# stay silent.
#
# Steps:
#   1. Load module-null-sink "remote_audio" if not already loaded.
#   2. Set remote_audio as the default sink.
#   3. If a RustDesk source-output is active, move it to remote_audio.monitor.
#
# Idempotent — safe to re-run.
#
# Replaces the auto-routing block that used to live inside start_workflow.sh.
# That block fired on every workflow start and silenced local speakers even
# when no remote session was active (2026-05-19 audio incident).

set -u

GREEN=$'\033[1;32m'
RED=$'\033[1;31m'
DIM=$'\033[2m'
RESET=$'\033[0m'

ok()   { printf "${GREEN}OK${RESET}    %s\n" "$1"; }
bad()  { printf "${RED}FAIL${RESET}  %s\n" "$1"; }
info() { printf "${DIM}      %s${RESET}\n" "$1"; }

echo "== rustdesk_audio_on =="

# 1. Load null sink if not present
if pactl list short sinks 2>/dev/null | grep -q $'\tremote_audio\t'; then
  ok "null sink 'remote_audio' already loaded"
else
  if pactl load-module module-null-sink sink_name=remote_audio \
       sink_properties='device.description="RemoteAudio"' >/dev/null 2>&1; then
    ok "null sink 'remote_audio' loaded"
  else
    bad "could not load module-null-sink (PulseAudio not running?)"
    exit 1
  fi
fi

# 2. Set as default sink
if pactl set-default-sink remote_audio >/dev/null 2>&1; then
  ok "default sink set to remote_audio"
else
  bad "could not set default sink"
fi

# 3. If RustDesk is capturing, move its source-output to remote_audio.monitor
rustdesk_so=$(pactl list source-outputs 2>/dev/null \
  | awk '/^Source Output #/{id=$3; sub("#","",id)} /application.name = "RustDesk"/{print id; exit}')

if [[ -z "$rustdesk_so" ]]; then
  info "no active RustDesk capture (run RustDesk first if expected)"
else
  if pactl move-source-output "$rustdesk_so" remote_audio.monitor >/dev/null 2>&1; then
    ok "moved RustDesk capture (so #$rustdesk_so) to remote_audio.monitor"
  else
    info "RustDesk capture already on remote_audio.monitor"
  fi
fi
