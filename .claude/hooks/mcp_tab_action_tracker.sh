#!/usr/bin/env bash
# PostToolUse: tracks the last sensei action so mcp_tab_only_guard can verify
# that browse is always preceded by tab_create.

TOOL_NAME="${CLAUDE_TOOL_NAME:-}"

case "$TOOL_NAME" in
  mcp__sensei__tab_create)
    echo "tab_create" > /tmp/.last_sensei_action
    ;;
  mcp__sensei__browse)
    # After a successful browse, reset so next browse requires a new tab_create
    echo "browse" > /tmp/.last_sensei_action
    ;;
  mcp__sensei__*)
    # Any other sensei tool call clears the tab_create flag
    # (navigate/click/read etc. are fine within an existing tab)
    ;;
esac

exit 0
