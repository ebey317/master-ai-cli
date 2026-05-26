#!/usr/bin/env bash
# _compile_policy.sh — v1.1
# Sync ~/.claude/.retry_policy.json from retry_policy.yaml (no jq dep in YAML source).
# v1.1 addition: emit .retry_policy.json.sha256 alongside JSON for tamper detection.
set -euo pipefail

YAML="$HOME/.claude/retry_policy.yaml"
JSON="$HOME/.claude/.retry_policy.json"
SUM="$HOME/.claude/.retry_policy.json.sha256"

if [[ ! -f "$YAML" ]]; then
    echo "ERROR: $YAML not found" >&2
    exit 1
fi

if command -v yq >/dev/null 2>&1; then
    yq eval -o=json "$YAML" > "$JSON.tmp" && mv "$JSON.tmp" "$JSON"
    echo "compiled (yq): $YAML → $JSON"
elif command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" 2>/dev/null; then
    python3 -c "
import yaml, json, sys
with open('$YAML') as f:
    data = yaml.safe_load(f)
with open('$JSON', 'w') as f:
    json.dump(data, f, indent=2)
"
    echo "compiled (python3 + pyyaml): $YAML → $JSON"
else
    echo "ERROR: need yq OR python3+pyyaml to compile" >&2
    exit 1
fi

# v1.1: emit sha256 checksum alongside JSON for tamper detection
sha256sum "$JSON" | cut -d' ' -f1 > "$SUM"
echo "checksum: $(cat "$SUM") → $SUM"
