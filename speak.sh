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

ENGINE="${SPEAK_ENGINE:-espeak}"
RATE="${SPEAK_RATE:-175}"
PIPER_MODEL="${SPEAK_PIPER_MODEL:-$HOME/.local/share/piper/en_US-amy-medium.onnx}"

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
    echo "$TEXT" | espeak -s "$RATE" -v en-us --stdout | paplay
    ;;
  piper)
    if [[ ! -f "$PIPER_MODEL" ]]; then
      echo "speak: piper model not found at $PIPER_MODEL" >&2
      echo "  Download a voice: https://github.com/rhasspy/piper/blob/master/VOICES.md" >&2
      exit 1
    fi
    echo "$TEXT" | piper -m "$PIPER_MODEL" --output-raw 2>/dev/null \
      | aplay -r 22050 -f S16_LE -t raw - 2>/dev/null
    ;;
  spd-say)
    spd-say -r "$((RATE - 175))" "$TEXT"
    ;;
  *)
    echo "speak: unknown engine '$ENGINE' (use espeak, piper, or spd-say)" >&2
    exit 1
    ;;
esac
