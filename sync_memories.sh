#!/usr/bin/env bash
# Memory sync helper — integrate into your startup pipeline.
# 
# Usage:
#   bash ~/scripts/sync_memories.sh login <username> <api_key>
#   bash ~/scripts/sync_memories.sh pull    # fetch remote
#   bash ~/scripts/sync_memories.sh push    # upload local
#   bash ~/scripts/sync_memories.sh sync    # two-way merge

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

# Ensure memory_sync.py is available
if [ ! -f "$SCRIPTS_DIR/memory_sync.py" ]; then
    echo "ERROR: memory_sync.py not found in $SCRIPTS_DIR"
    exit 1
fi

CMD="${1:-sync}"

case "$CMD" in
    login)
        if [ $# -lt 3 ]; then
            echo "Usage: $0 login <username> <api_key>"
            exit 1
        fi
        USERNAME="$2"
        API_KEY="$3"
        echo "[sync] Logging in as $USERNAME..."
        $PYTHON "$SCRIPTS_DIR/memory_sync.py" --login "$USERNAME" "$API_KEY"
        ;;
    
    pull)
        echo "[sync] Pulling remote memories..."
        $PYTHON "$SCRIPTS_DIR/memory_sync.py" --pull
        ;;
    
    push)
        echo "[sync] Pushing local memories..."
        $PYTHON "$SCRIPTS_DIR/memory_sync.py" --push
        ;;
    
    sync)
        echo "[sync] Two-way sync..."
        $PYTHON "$SCRIPTS_DIR/memory_sync.py" --sync
        ;;
    
    auto)
        # Auto-sync on startup (runs if enough time has passed)
        $PYTHON "$SCRIPTS_DIR/memory_sync.py" --sync-if-needed
        ;;
    
    *)
        echo "Unknown command: $CMD"
        echo "Available: login, pull, push, sync, auto"
        exit 1
        ;;
esac
