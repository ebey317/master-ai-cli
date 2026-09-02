#!/bin/bash
# ============================================================
# MASTER AI — PACK FOR SALE
# Produces a clean buyer tarball from the local repo, scrubbing
# operator-specific files, keys, logs, and git history.
#
# Run from the repo root:
#   bash pack_for_sale.sh
# Output: ./dist/master-ai-v<VERSION>.tar.gz
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_NAME="$(basename "$SCRIPT_DIR")"
VERSION="${VERSION:-$(date +%Y%m%d)}"
DIST_DIR="$SCRIPT_DIR/dist"
STAGE_DIR="$DIST_DIR/stage/master-ai-$VERSION"
OUTPUT="$DIST_DIR/master-ai-v$VERSION.tar.gz"
mkdir -p "$DIST_DIR/stage"

# 1. Copy repo into staging (exclude known operator/secret/runtime files)
rsync -a \
  --exclude=".git" \
  --exclude=".gitignore" \
  --exclude="*.log" \
  --exclude="*.pyc" \
  --exclude="__pycache__" \
  --exclude=".master_ai_keys" \
  --exclude="*.key" --exclude="*.pem" \
  --exclude="client_secret*.json" \
  --exclude="*token*.json" \
  --exclude="*.env" --exclude=".env" \
  --exclude="sessions/" \
  --exclude=".cache" \
  --exclude="harvest.cache" \
  --exclude="dist/" \
  --exclude="master-ai-v*.tar.gz" \
  --exclude="image_engine/models/" \
  --exclude="image_engine/stable-diffusion.cpp/" \
  --exclude="image_engine/out/" \
  --exclude="memory/" \
  --exclude=".claude/" \
  --exclude="*.egg-info/" \
  "$SCRIPT_DIR/" "$STAGE_DIR/"

# 2. Strip all lines containing literal secrets / API keys patterns
#    This is a safety pass; real keys should not be in source anyway.
find "$STAGE_DIR" -type f \( -name "*.py" -o -name "*.sh" -o -name "*.md" -o -name "*.json" -o -name "*.yaml" -o -name "*.yml" -o -name "*.txt" \) \
  -exec grep -lE "(sk-ant-|sk-or-v1-|sk-proj-|gsk_|AIzaSy|hf_|xai-|nvapi-|AKIA[A-Z0-9]{16})" {} \; 2>/dev/null | while read -r f; do
    echo "⚠ Removing suspected secret line from $f"
    sed -i -E "/(sk-ant-|sk-or-v1-|sk-proj-|gsk_|AIzaSy|hf_|xai-|nvapi-|AKIA[A-Z0-9]{16})/d" "$f"
done

# 3. Assert no PII/secret leftovers in key files
leftovers=$(find "$STAGE_DIR" -type f \( -name "*.py" -o -name "*.sh" -o -name "*.md" -o -name "*.json" \) \
  -exec grep -lE "(sk-ant-|sk-or-v1-|sk-proj-|gsk_|AIzaSy|hf_|xai-|nvapi-|AKIA[A-Z0-9]{16})" {} \; 2>/dev/null || true)
if [ -n "$leftovers" ]; then
    echo "✗ Secret patterns still found after scrub:"
    echo "$leftovers"
    exit 1
fi

# 4. Clean-machine install test in a temp HOME
TEST_HOME="$(mktemp -d)"
trap 'rm -rf "$TEST_HOME"' EXIT

export HOME="$TEST_HOME"
# Copy install.sh into a standalone location to simulate buyer machine
mkdir -p "$TEST_HOME/buyer"
cp -R "$STAGE_DIR/." "$TEST_HOME/buyer/"
chmod +x "$TEST_HOME/buyer/install.sh"

# Run install in fully non-interactive auto-approve mode (Ollama/model pulls skipped)
export APPROVE_ALL=1
export MASTER_AI_NONINTERACTIVE=1
export SKIP_OLLAMA=1
export SKIP_MODELS=1
if bash "$TEST_HOME/buyer/install.sh" >"$TEST_HOME/install-test.log" 2>&1; then
    echo "✓ Clean-machine install test passed"
else
    echo "✗ Install test failed. Log: $TEST_HOME/install-test.log"
    tail -50 "$TEST_HOME/install-test.log"
    exit 1
fi

# Verify expected runtime dirs and commands exist
for d in .master_ai_profiles/default .master_ai_skills .master_ai_mcp .master_ai_logs .master_ai_skins; do
    [ -d "$TEST_HOME/$d" ] || { echo "✗ Missing dir: $d"; exit 1; }
done
for cmd in .local/bin/master .local/bin/sensei; do
    [ -x "$TEST_HOME/$cmd" ] || { echo "✗ Missing command: $cmd"; exit 1; }
done

echo "✓ Verified buyer install creates commands and runtime dirs"

# 5. Tar it up
tar -czf "$OUTPUT" -C "$DIST_DIR/stage" "master-ai-$VERSION"
echo ""
echo "✅ Packaged: $OUTPUT"
echo "   Size: $(du -h "$OUTPUT" | cut -f1)"
echo "   Contents: master-ai-$VERSION/"

# 6. Optional: manifest
find "$STAGE_DIR" -type f | sed "s|$STAGE_DIR/||" | sort > "$DIST_DIR/master-ai-v$VERSION.manifest.txt"
echo "   Manifest: $DIST_DIR/master-ai-v$VERSION.manifest.txt"
