#!/usr/bin/env bash
# Speak text aloud — reads from clipboard if no argument given, else speaks the
# argument(s). Uses espeak as the default engine (instant, no model download).
# Swap to piper (neural, natural) by setting SPEAK_ENGINE=piper and pointing
# SPEAK_PIPER_MODEL at a downloaded voice .onnx file.
#
# Usage:
#   speak.sh                       # speaks whatever is on the clipboard
#   speak.sh "hello world"         # speaks the argument
#   echo "hi" | speak.sh           # speaks stdin
#   SPEAK_RATE=180 speak.sh        # adjust espeak speech rate (default 175)
#
# Designed to be called from a global keyboard shortcut OR directly. Pairs
# well with: highlight text on screen → clipboard auto-syncs → run speak.sh.

set -euo pipefail

PIPER_MODEL="${SPEAK_PIPER_MODEL:-$HOME/.local/share/piper/en_US-joe-medium.onnx}"
# Default to piper if the model exists (neural, sounds human). Fall back to
# espeak (instant, robotic) if not. Override with SPEAK_ENGINE=espeak|spd-say.
if [[ -z "${SPEAK_ENGINE:-}" ]]; then
  if [[ -f "$PIPER_MODEL" ]]; then
    ENGINE="piper"
  else
    ENGINE="espeak"
  fi
else
  ENGINE="$SPEAK_ENGINE"
fi
RATE="${SPEAK_RATE:-175}"
# Force a specific PulseAudio sink so stream-restore doesn't hijack TTS
# back to remote_audio. Default to the DCR006x_BT speaker; override with
# SPEAK_SINK=<sink-name> or set to "" to fall back to default-sink.
SPEAK_SINK="${SPEAK_SINK-bluez_sink.C8_47_8C_01_6A_10.a2dp_sink}"
PAPLAY_DEV_FLAG=()
if [[ -n "$SPEAK_SINK" ]]; then
  PAPLAY_DEV_FLAG=(-d "$SPEAK_SINK")
fi

read_text() {
  if [[ $# -gt 0 ]]; then
    printf '%s' "$*"
  elif ! [ -t 0 ]; then
    cat
  else
    # Try X11 clipboard first (xclip), then xsel as fallback
    if command -v xclip >/dev/null; then
      xclip -selection clipboard -o 2>/dev/null
    elif command -v xsel >/dev/null; then
      xsel -b 2>/dev/null
    else
      echo "ERROR: no clipboard reader (install xclip or xsel)" >&2
      return 1
    fi
  fi
}

TEXT="$(read_text "$@")"
if [[ -z "$TEXT" ]]; then
  echo "speak: nothing to say (clipboard empty?)" >&2
  exit 1
fi

case "$ENGINE" in
  espeak)
    echo "$TEXT" | espeak -s "$RATE" -v en-us --stdout | paplay "${PAPLAY_DEV_FLAG[@]}"
    ;;
  piper)
    if [[ ! -f "$PIPER_MODEL" ]]; then
      echo "speak: piper model not found at $PIPER_MODEL" >&2
      echo "  Download a voice: https://github.com/rhasspy/piper/blob/master/VOICES.md" >&2
      exit 1
    fi
    # Pipe through paplay (PulseAudio) NOT aplay (raw ALSA) so output flows
    # through the BT sink (or default if SPEAK_SINK="") — overrides stream-restore.
    echo "$TEXT" | piper -m "$PIPER_MODEL" --output-raw 2>/dev/null \
      | paplay "${PAPLAY_DEV_FLAG[@]}" --raw --rate=22050 --format=s16le --channels=1
    ;;
  spd-say)
    spd-say -r "$((RATE - 175))" "$TEXT"
    ;;
  *)
    echo "speak: unknown engine '$ENGINE' (use espeak, piper, or spd-say)" >&2
    exit 1
    ;;
esac
