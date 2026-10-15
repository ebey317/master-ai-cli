#!/usr/bin/env bash
# Example shell hook that escalates risky terminal commands to human approval.
# Save as executable script and register in Sensei hooks config under pre_tool_call.
#
# Usage: The hook receives tool_name and tool_input via stdin as JSON.
# It prints a single JSON line to stdout with the directive.

set -euo pipefail

# Read the hook input (tool_name, tool_input, etc.)
input_json=$(cat)

# Extract tool name and command using jq (install if missing)
tool_name=$(echo "$input_json" | jq -r '.tool_name // "unknown"')
command=$(echo "$input_json" | jq -r '.tool_input.command // ""')

# Example policy: escalate rm -rf and sudo commands
if [[ "$tool_name" == "terminal" ]]; then
    if [[ "$command" =~ ^sudo ]] || [[ "$command" =~ rm.*-rf ]]; then
        # Emit approve directive with context
        printf '{"action": "approve", "message": "Command requires human confirmation: %s", "rule_key": "terminal:dangerous"}\n' "$command"
        exit 0
    fi
fi

# No directive → tool proceeds normally
exit 0
