#!/usr/bin/env bash
# operator_visibility_guard.sh
#
# Enforces the standing rule: operator MUST see every action that uses his
# authenticated login credentials (Canva, Drive, Gmail, sensei, etc.).
# Memory: ~/.claude/projects/-home-elijah/memory/feedback_operator_must_see_authenticated_actions.md
#
# Wired as a PreToolUse hook in ~/.claude/settings.json. Claude Code pipes
# the tool-call JSON to this script's stdin.
#
# Exit codes:
#   0 = allow the tool call to proceed
#   2 = BLOCK (Claude Code aborts the call and shows the JSON reason)
#
# Lockdown:  touch /tmp/operator_lockdown_authenticated   (blocks every authenticated CHANGE)
# Approve one destructive op:  touch /tmp/operator_destructive_approved
# Suppress TTS for a quiet hour:  touch /tmp/operator_quiet
#
set -euo pipefail

TOOL_INPUT="$(cat)"
TOOL_NAME=$(printf '%s' "$TOOL_INPUT" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(d.get("tool_name",""))
except Exception:
    print("")' 2>/dev/null || true)

LOG_FILE="$HOME/.claude/operator_visibility.log"
TS=$(date '+%Y-%m-%d %H:%M:%S')

# --- 1. Only inspect authenticated-MCP tools. Everything else passes silently. ---
AUTH_RE='^mcp__(canva|claude_ai_Canva|claude_ai_Google_Drive|claude_ai_Gmail|claude_ai_Google_Calendar|claude_ai_Todoist|claude_ai_Indeed|claude_ai_ZipRecruiter|claude_ai_Hugging_Face|hugging-face|claude_ai_Base44|sensei|claude-in-chrome|secretary)__'

if ! [[ "$TOOL_NAME" =~ $AUTH_RE ]]; then
    exit 0
fi

# --- 2. Classify the tool. ---
READ_RE='__(search|find|list|get|read|query|fetch|health|view|resolve|describe|hub_repo|hf_doc|paper_search|space_search|hf_whoami|fetch_object|user-info|get-overview|get-thread|list-comments|list-replies|find-comments|find-reminders|find-tasks|find-projects|find-sections|find-goals|find-labels|find-filters|find-activity|find-completed|find-project-collaborators)'
DESTRUCTIVE_RE='__(delete|remove|trash|drop|cancel-editing-transaction|unlabel|delete-event|delete-object)'
WRITE_RE='__(create|add|update|edit|modify|commit|perform-editing|send|post|reply|comment|move|copy|upload|click|fill|run|navigate|scroll|key|type|left_click|right_click|double_click|triple_click|hover|left_click_drag|file_upload|reorder|complete|uncomplete|archive|unarchive|reschedule|manage|insert|generate-design|import-design|export-design|resize-design|copy-design|create-design|update-label|create-label|tabs_create|tabs_close|form_input|javascript|computer|browser_batch|js_eval|write_file|generate|secretary_intake|secretary_run|secretary_resume|secretary_cancel|secretary_pause|create_base44|edit_base44|create_entities|update_entities|create_entity_schema|update_entity_schema|label_thread|label_message|create_draft|create_event|update_event|respond_to_event|copy_file)'

# --- 3. Read-only: log + allow. ---
if [[ "$TOOL_NAME" =~ $READ_RE ]]; then
    printf '[%s] READ   %s\n' "$TS" "$TOOL_NAME" >> "$LOG_FILE"
    exit 0
fi

# --- 4. Lockdown short-circuit. ---
if [[ -f /tmp/operator_lockdown_authenticated ]]; then
    printf '[%s] LOCKDOWN %s\n' "$TS" "$TOOL_NAME" >> "$LOG_FILE"
    cat <<EOF
{
  "decision": "block",
  "reason": "BLOCKED: operator lockdown active (/tmp/operator_lockdown_authenticated exists). Tool $TOOL_NAME is an authenticated CHANGE and cannot fire until lockdown is lifted. To lift: rm /tmp/operator_lockdown_authenticated"
}
EOF
    exit 2
fi

# --- 5. Destructive: require one-shot approval. ---
if [[ "$TOOL_NAME" =~ $DESTRUCTIVE_RE ]]; then
    if [[ ! -f /tmp/operator_destructive_approved ]]; then
        printf '[%s] BLOCK-DESTRUCTIVE %s\n' "$TS" "$TOOL_NAME" >> "$LOG_FILE"
        # Try to alert operator audibly if speak.sh exists
        if [[ -x "$HOME/scripts/speak.sh" && ! -f /tmp/operator_quiet ]]; then
            "$HOME/scripts/speak.sh" "Destructive action blocked. Need your approval." >/dev/null 2>&1 &
        fi
        cat <<EOF
{
  "decision": "block",
  "reason": "BLOCKED: destructive action on operator's account. Tool: $TOOL_NAME. To approve ONE destructive call, run:  touch /tmp/operator_destructive_approved  — then retry. Rule: feedback_operator_must_see_authenticated_actions."
}
EOF
        exit 2
    fi
    rm -f /tmp/operator_destructive_approved
    printf '[%s] APPROVED-DESTRUCTIVE %s\n' "$TS" "$TOOL_NAME" >> "$LOG_FILE"
    # Fall through to write narration below
fi

# --- 6. State-changing on operator's account: narrate loudly, then allow. ---
if [[ "$TOOL_NAME" =~ $WRITE_RE ]] || [[ "$TOOL_NAME" =~ $DESTRUCTIVE_RE ]]; then
    printf '[%s] WRITE  %s\n' "$TS" "$TOOL_NAME" >> "$LOG_FILE"

    # Audible alert (suppressible via /tmp/operator_quiet)
    if [[ -x "$HOME/scripts/speak.sh" && ! -f /tmp/operator_quiet ]]; then
        SHORT=$(printf '%s' "$TOOL_NAME" | sed 's/^mcp__//; s/__/ /g' | head -c 80)
        "$HOME/scripts/speak.sh" "Acting on your account: $SHORT" >/dev/null 2>&1 &
    fi

    # Loud stderr banner — Claude Code shows hook stderr to the operator
    {
        echo ""
        echo "════════════════════════════════════════════════════"
        echo "  AUTHENTICATED ACTION FIRING ON YOUR ACCOUNT"
        echo "  Tool:  $TOOL_NAME"
        echo "  Time:  $TS"
        echo "  Rule:  feedback_operator_must_see_authenticated_actions"
        echo "════════════════════════════════════════════════════"
        echo ""
    } >&2

    exit 0
fi

# --- 7. Authenticated tool that didn't match any classification (novel verb). ---
# Log + narrate as a write to err on the side of caution.
printf '[%s] UNCLASSIFIED %s\n' "$TS" "$TOOL_NAME" >> "$LOG_FILE"
{
    echo ""
    echo "[visibility_guard] UNCLASSIFIED authenticated tool: $TOOL_NAME — allowing but flagged for review." >&2
    echo ""
} >&2
exit 0
