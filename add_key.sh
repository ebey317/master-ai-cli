#!/usr/bin/env bash
# add_key.sh — blindfold-compliant key adder.
# Prompts for key name + value (value hidden with read -s, never echoed),
# atomically merges into ~/.master_ai_keys, chmod 600. Never logs the value.

set -euo pipefail

KEYS_FILE="$HOME/.master_ai_keys"
TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

# Ensure file exists with valid JSON
if [ ! -f "$KEYS_FILE" ]; then
    echo "{}" > "$KEYS_FILE"
    chmod 600 "$KEYS_FILE"
fi

# Confirm it's valid JSON before we touch it
python3 -c "import json,sys; json.load(open('$KEYS_FILE'))" 2>/dev/null || {
    echo "ERR: $KEYS_FILE is not valid JSON. Edit it manually first." >&2
    exit 1
}

read -rp "Key slug (e.g. groq, gemini, openrouter): " SLUG
if [ -z "$SLUG" ]; then
    echo "Empty slug, aborting." >&2
    exit 1
fi

read -srp "Value (hidden — paste, then press Enter): " VALUE
echo
if [ -z "$VALUE" ]; then
    echo "Empty value, aborting." >&2
    exit 1
fi

# Merge atomically via python (preserves JSON shape, redacts value from process listing)
python3 - "$SLUG" <<EOF >"$TMP_FILE"
import json, os, sys
slug = sys.argv[1]
val = os.environ['_K']
with open("$KEYS_FILE") as f:
    d = json.load(f)
d[slug] = val
print(json.dumps(d, indent=2))
EOF
_K="$VALUE" python3 - "$SLUG" <<EOF >"$TMP_FILE"
import json, os, sys
slug = sys.argv[1]
val = os.environ['_K']
with open("$KEYS_FILE") as f:
    d = json.load(f)
d[slug] = val
print(json.dumps(d, indent=2))
EOF
unset VALUE _K

mv "$TMP_FILE" "$KEYS_FILE"
chmod 600 "$KEYS_FILE"
echo "Added/updated key '$SLUG' in $KEYS_FILE"
echo "Run 'python3 ~/scripts/validate_keys.py' to verify it works."
