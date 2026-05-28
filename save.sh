#!/usr/bin/env bash
# save.sh — Complete save procedure
# Commits git, updates office spreadsheet, splices account context to memory
# Usage: ./save.sh "commit message" or just: save (uses default message)

set -euo pipefail

HOME_DIR="$HOME"
HC="$HOME_DIR/.claude"
PROJECTS="$HOME_DIR/projects"
SCRIPTS="$HOME_DIR/scripts"
MEMORY_DIR="$HC/projects/-home-elijah/memory"
OFFICE="$HOME_DIR/Desktop/MasterAI_Office.ods"
SESSION_CONTEXT="$MEMORY_DIR/project_session_account_context.md"

# ── DETECT ACCOUNT SIDE ────────────────────────────────────────────────────
detect_account_side() {
    # Priority 1: check environment variables
    if [[ -n "${ANTHROPIC_CONSOLE_KEY:-}" ]]; then
        echo "API/CLAF"
        return 0
    fi

    # Priority 2: check if launch.sh was sourced or CLAF settings exist
    if [[ -n "${CLAF_ACTIVE:-}" ]] || [[ -f "$PROJECTS/claf/.claude/settings.json" && -n "$(jq -r '.model // ""' "$PROJECTS/claf/.claude/settings.json" 2>/dev/null)" ]]; then
        echo "API/CLAF"
        return 0
    fi

    # Priority 3: assume Pro if Pro settings exist
    if [[ -f "$HC/settings.json" ]]; then
        echo "Pro"
        return 0
    fi

    echo "Unknown"
}


# ── DETECT ACTIVE MODEL ────────────────────────────────────────────────────
detect_active_model() {
    local side="$1"
    local settings_path

    if [[ "$side" == "API/CLAF" ]]; then
        settings_path="$PROJECTS/claf/.claude/settings.json"
    else
        settings_path="$HC/settings.json"
    fi

    if [[ -f "$settings_path" ]]; then
        jq -r '.model // "unknown"' "$settings_path" 2>/dev/null || echo "unknown"
    else
        echo "not-found"
    fi
}

# ── COMMIT GIT ────────────────────────────────────────────────────────────
commit_git() {
    local msg="${1:-Update portfolio, projects, and account context}"

    if [[ -z "$(git status --short)" ]]; then
        echo "✓ Git: nothing to commit (working tree clean)"
        return 0
    fi

    git add -A
    git commit -m "$msg"
    echo "✓ Git: committed"
}

# ── UPDATE OFFICE SPREADSHEET ──────────────────────────────────────────────
update_office() {
    # Read current portfolio state and write to ODS
    # For now, this is a placeholder — full ODS update requires python-odf
    # Basic version: just ensure the file exists
    if [[ -f "$OFFICE" ]]; then
        echo "✓ Office: $OFFICE exists (full update requires python3-odf)"
    else
        echo "⚠ Office: $OFFICE not found"
    fi
}

# ── SPLICE ACCOUNT CONTEXT TO MEMORY ───────────────────────────────────────
splice_account_context() {
    local account_side="$1"
    local active_model="$2"
    local ts=$(date '+%Y-%m-%dT%H:%M:%SZ')

    # Detect if on API side or Pro side
    local settings_path auth_desc base_url

    if [[ "$account_side" == "API/CLAF" ]]; then
        settings_path="~/projects/claf/.claude/settings.json"
        auth_desc="ANTHROPIC_CONSOLE_KEY in keychain"
        base_url="http://localhost:8000"
    else
        settings_path="~/.claude/settings.json"
        auth_desc="OAuth (claude.ai Max)"
        base_url="https://api.anthropic.com"
    fi

    # Update the session context memory file
    cat > "$SESSION_CONTEXT" << EOF
---
name: session-account-context
description: "Current account context — which side (Pro vs API/CLAF), active model, billing status. Auto-updated on every save."
metadata:
  type: project
  updated_ts: "$ts"
---

# Session Account Context

**Last save:** $(date '+%Y-%m-%d %H:%M %p')
**Current account side:** $account_side
**Active model:** $active_model
**Default settings:** $settings_path
**Auth:** $auth_desc
**Base URL:** $base_url

## Account Details
- Account side: **$account_side**
- Model: $active_model
- Settings: $settings_path
- Status: working tree clean

## Workflow
- Next session will auto-load this context
- No "which side?" friction — memory is spliced
EOF

    echo "✓ Memory: account context spliced ($account_side | $active_model)"
}

# ── MAIN ───────────────────────────────────────────────────────────────────
main() {
    local commit_msg="${1:-Update portfolio and account context}"

    echo "════════════════════════════════════════════════════════════════"
    echo "SAVE PROCEDURE — $(date '+%Y-%m-%d %H:%M:%S')"
    echo "════════════════════════════════════════════════════════════════"
    echo ""

    # Detect account side
    local account_side
    account_side="$(detect_account_side)"
    echo "Detecting account side: $account_side"

    # Detect active model
    local active_model
    active_model="$(detect_active_model "$account_side")"
    echo "Active model: $active_model"
    echo ""

    # Run save steps
    commit_git "$commit_msg"
    update_office
    splice_account_context "$account_side" "$active_model"

    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo "✓ SAVE COMPLETE"
    echo "════════════════════════════════════════════════════════════════"
}

main "$@"
