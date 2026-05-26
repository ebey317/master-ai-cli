#!/usr/bin/env bash
# claf_bridge.sh — CLAF ↔ Claude Code Console bridge hook
# Registered as: UserPromptSubmit hook in ~/.claude/settings.json
#
# Fires on every user message. Checks CLAF proxy health and routing status,
# then injects one line into the model's system prompt:
#
#   [CLAF OK]     — proxy running, Claude Code is routed through it
#   [CLAF BYPASS] — proxy running but Claude Code is going DIRECT to Anthropic
#                   (ANTHROPIC_BASE_URL not set = no launch.sh was used)
#   [CLAF OFFLINE]— proxy not reachable; all traffic goes direct to Anthropic
#
# The BYPASS case is the billing risk: traffic goes direct, consuming API credits
# even when CLAF's free tiers have capacity. Fix: run launch.sh in a new terminal.
#
# Exit 0 always — informational, never blocks.

set -euo pipefail

PROXY_URL="${CLAF_PROXY_URL:-http://localhost:8000}"
TIMEOUT=1   # fast fail — don't stall the session for a dead proxy

# ── Check if CLAF is reachable ────────────────────────────────────────────────
STATS=$(curl -fsS --max-time "$TIMEOUT" "${PROXY_URL}/stats" 2>/dev/null || echo "")

if [[ -z "$STATS" ]]; then
    # Proxy unreachable
    MSG="[CLAF OFFLINE] — proxy not responding at ${PROXY_URL}. All traffic is direct Anthropic (billing risk). Run: python3 ~/projects/claf/orchestrator.py"
    printf '{"hookSpecificReturn":{"additionalSystemPrompt":"%s"}}\n' "$MSG"
    exit 0
fi

# ── Parse routing stats ───────────────────────────────────────────────────────
ROUTING=$(python3 -c "
import json, sys
try:
    d = json.loads('''$STATS''')
except Exception as e:
    print('parse_error')
    sys.exit(0)

by_tier = d.get('by_tier', {})
totals  = d.get('totals', {})
total   = totals.get('total_calls', 0)

t0  = by_tier.get('0',  {}).get('calls', 0)   # local Ollama
t1  = by_tier.get('1',  {}).get('calls', 0)   # cloud-free peer
t_a = by_tier.get('9',  {}).get('calls', 0)   # anthropic peer (tier 9 in config)
# Also check legacy tier 6 label
t_a6 = by_tier.get('6', {}).get('calls', 0)
anthropic = max(t_a, t_a6)

offgrid = total - anthropic
pct     = (100 * offgrid // total) if total else 0

throttle = d.get('throttle', {})
flash_rem = throttle.get('flash', {}).get('remaining', '?')
tap_rem   = throttle.get('tap',   {}).get('remaining', '?')

print(f'local={t0} cloud-free={t1} anthropic={anthropic} | offgrid={pct}% | flash_left={flash_rem} tap_left={tap_rem}')
" 2>/dev/null || echo "stats_unavailable")

# ── Detect bypass: CLAF is up but Claude Code isn't routed through it ─────────
# launch.sh sets ANTHROPIC_BASE_URL; if absent, Claude Code goes direct OAuth.
if [[ -z "${ANTHROPIC_BASE_URL:-}" ]]; then
    BYPASS_WARN=" ⚠️  BYPASS ACTIVE — ANTHROPIC_BASE_URL not set. Claude Code is NOT going through CLAF (direct OAuth/API). Run: bash ~/projects/claf/launch.sh"
    MSG="[CLAF BYPASS] proxy=UP routing=${ROUTING}${BYPASS_WARN}"
else
    if [[ "$ANTHROPIC_BASE_URL" == *"localhost"* ]] || [[ "$ANTHROPIC_BASE_URL" == *"127.0.0.1"* ]]; then
        MSG="[CLAF OK] base=${ANTHROPIC_BASE_URL} routing=${ROUTING}"
    else
        MSG="[CLAF WARN] ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL} (not localhost — is this intentional?). routing=${ROUTING}"
    fi
fi

# Escape for JSON
MSG="${MSG//\\/\\\\}"
MSG="${MSG//\"/\\\"}"

printf '{"hookSpecificReturn":{"additionalSystemPrompt":"%s"}}\n' "$MSG"
exit 0
