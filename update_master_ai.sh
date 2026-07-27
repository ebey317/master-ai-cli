#!/bin/bash
# Customer-safe Master AI updater.
# This is deliberately not a git updater. Customer installs may not be git
# repos, and "update Master AI" must never turn into `git fetch --all`.
set -euo pipefail

HOME_DIR="${HOME:-/home/user}"
SCRIPTS_DIR="$HOME_DIR/scripts"
BACKUP_ROOT="$HOME_DIR/.master_ai_update_backups"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"
RELEASE_URL="${MASTER_AI_UPDATE_URL:-}"

echo "Master AI updater"
echo "Mode: customer-safe release update"
echo

mkdir -p "$BACKUP_DIR"

backup_path() {
    local path="$1"
    if [ -e "$path" ]; then
        cp -a "$path" "$BACKUP_DIR/" 2>/dev/null || true
        echo "  backed up: $path"
    fi
}

echo "[1/5] Preserving customer data"
backup_path "$HOME_DIR/.master_ai_keys"
backup_path "$HOME_DIR/.master_ai_chats"
backup_path "$HOME_DIR/.master_ai_profiles"
backup_path "$HOME_DIR/.master_ai_router_metrics.jsonl"
backup_path "$SCRIPTS_DIR/master_ai_voice.json"
backup_path "$SCRIPTS_DIR/PROJECTS.md"

echo
echo "[2/5] Checking release source"
if [ -n "$RELEASE_URL" ]; then
    tmp="$(mktemp -d -t master_ai_update_XXXXXX)"
    archive="$tmp/master-ai-release.tgz"
    echo "  downloading: $RELEASE_URL"
    curl -fL "$RELEASE_URL" -o "$archive"
    echo "  unpacking into: $SCRIPTS_DIR"
    tar -xzf "$archive" -C "$SCRIPTS_DIR"
    rm -rf "$tmp"
else
    echo "  no MASTER_AI_UPDATE_URL set; skipped release download"
    echo "  local code on disk will be refreshed and verified"
fi

echo
echo "[3/5] Rebuilding local Sensei model"
if command -v ollama >/dev/null 2>&1 && [ -f "$SCRIPTS_DIR/Modelfile-master-ai" ]; then
    ollama create master-ai -f "$SCRIPTS_DIR/Modelfile-master-ai"
else
    echo "  skipped: ollama or Modelfile-master-ai missing"
fi

echo
echo "[4/5] Refreshing Sensei (deferred until update completes)"
# Defer the SIGTERM so this script — which Sensei dispatched — finishes
# cleanly before its parent dies. Without this, pkill kills the running
# Sensei mid-dispatch and the user sees "Terminated" + exit 1 on the
# very command they ran.
trap '(nohup bash -c "sleep 2 && pkill -TERM -f \"python3.*master_ai.py\"" >/dev/null 2>&1 &) || true' EXIT

echo
echo "[5/5] Health check"
if [ -x "$SCRIPTS_DIR/sensei_selftest.sh" ]; then
    bash "$SCRIPTS_DIR/sensei_selftest.sh"
else
    echo "  skipped: sensei_selftest.sh missing"
fi

echo
echo "Update flow complete."
echo "Backup: $BACKUP_DIR"
if [ -z "$RELEASE_URL" ]; then
    echo "To enable real customer release downloads, set MASTER_AI_UPDATE_URL to a release .tgz URL."
fi
