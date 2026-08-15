#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Master AI — API key setup (bash onboarding prompt)
#
# Interactive prompt for pasting provider API keys. Keys are stored in
# ~/.master_ai_keys (JSON, chmod 600) — the same file master_ai.py reads at
# startup via load_keys(). Routing (detect_route) auto-detects which
# providers you configured and picks the best lane:
#
#   no keys at all   -> local Ollama lane (CLAF local-first)
#   groq key         -> fast cloud lane (llama-3.3-70b)
#   openrouter key   -> deep/reasoning + fallback lane
#   gemini key       -> free-tier vision/cloud lane
#   fireworks key    -> cloud fallback lane
#   cerebras key     -> opt-in ultra-fast lane (`fast:` prefix)
#
# Usage:
#   bash setup_keys.sh                 interactive prompt
#   bash setup_keys.sh --check         verify stored keys against live APIs
#   bash setup_keys.sh --list          show which providers are configured
#   bash setup_keys.sh --remove NAME   delete one key from the store
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

KEYS_FILE="${MASTER_AI_KEYS_FILE:-$HOME/.master_ai_keys}"

# provider:display name:signup URL:validation method
PROVIDERS=(
    "groq|Groq (free, fast)|https://console.groq.com/keys|bearer:https://api.groq.com/openai/v1/models"
    "openrouter|OpenRouter (free models)|https://openrouter.ai/settings/keys|bearer:https://openrouter.ai/api/v1/auth/key"
    "gemini|Google Gemini (free tier)|https://aistudio.google.com/apikey|query:https://generativelanguage.googleapis.com/v1beta/models"
    "fireworks|Fireworks AI|https://fireworks.io/api-keys|bearer:https://api.fireworks.ai/inference/v1/models"
    "cerebras|Cerebras (ultra-fast)|https://cloud.cerebras.ai/platform|bearer:https://api.cerebras.ai/v1/models"
    "brave|Brave Search (web lane)|https://api-dashboard.search.brave.com/app/keys|header:https://api.search.brave.com/res/v1/web/search?q=test:X-Subscription-Token"
    "serper|Serper (Google search)|https://serper.dev/api-key|post:https://google.serper.dev/search:X-API-KEY"
)

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_bld=$'\033[1m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'

need_python() {
    command -v python3 >/dev/null 2>&1 || { echo "${c_red}python3 is required.${c_rst}" >&2; exit 1; }
}

store_read() {   # print current JSON (or {})
    if [[ -f "$KEYS_FILE" ]]; then cat "$KEYS_FILE"; else echo "{}"; fi
}

store_set() {    # store_set <name> <value>
    need_python
    python3 - "$KEYS_FILE" "$1" "$2" <<'PY'
import json, os, sys
path, name, value = sys.argv[1], sys.argv[2], sys.argv[3]
data = {}
if os.path.exists(path):
    try: data = json.load(open(path))
    except Exception: data = {}
data[name] = value
tmp = path + ".tmp"
with open(tmp, "w") as f: json.dump(data, f, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print(f"  saved '{name}' -> {path} (0600)")
PY
}

store_del() {    # store_del <name>
    need_python
    python3 - "$KEYS_FILE" "$1" <<'PY'
import json, os, sys
path, name = sys.argv[1], sys.argv[2]
try: data = json.load(open(path))
except Exception: data = {}
if name in data:
    del data[name]
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    print(f"  removed '{name}'")
else:
    print(f"  '{name}' not present")
PY
}

store_get() {    # store_get <name> -> value or empty
    python3 - "$KEYS_FILE" "$1" 2>/dev/null <<'PY' || true
import json, os, sys
try: d = json.load(open(sys.argv[1]))
except Exception: d = {}
v = d.get(sys.argv[2], "")
print(v if isinstance(v, str) else "")
PY
}

mask() {         # mask a key for display: first4****last4
    local v="$1"
    if [[ ${#v} -le 8 ]]; then echo "****"; else echo "${v:0:4}****${v: -4}"; fi
}

validate_key() { # validate_key <name> <value> -> returns 0 if live
    local name="$1" value="$2" spec method url hdr
    for spec in "${PROVIDERS[@]}"; do
        IFS='|' read -r pname _ _ method <<< "$spec"
        [[ "$pname" == "$name" ]] || continue
        case "$method" in
            bearer:*)
                url="${method#bearer:}"
                code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
                    -H "Authorization: Bearer $value" "$url" 2>/dev/null || echo 000)
                ;;
            query:*)
                url="${method#query:}"
                code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
                    "$url?key=$value" 2>/dev/null || echo 000)
                ;;
            header:*)
                url="${method#header:*:}" ; url="${method#header:}"; url="${url%%:*}"
                hdr="${method##*:}"
                code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
                    -H "$hdr: $value" "$url" 2>/dev/null || echo 000)
                ;;
            post:*)
                local rest="${method#post:}"; url="${rest%%:*}"; hdr="${rest##*:}"
                code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
                    -X POST -H "$hdr: $value" -H "Content-Type: application/json" \
                    -d '{"q":"test"}' "$url" 2>/dev/null || echo 000)
                ;;
            *) code="skip" ;;
        esac
        [[ "$code" == "200" || "$code" == "201" ]] && return 0
        echo "$code"
        return 1
    done
    return 2
}

cmd_list() {
    echo "${c_bld}Configured providers${c_rst} ($KEYS_FILE):"
    local any=0 spec name
    for spec in "${PROVIDERS[@]}"; do
        IFS='|' read -r name _ _ _ <<< "$spec"
        local v; v=$(store_get "$name")
        if [[ -n "$v" ]]; then echo "  ${c_grn}✓${c_rst} $name $(mask "$v")"; any=1
        else echo "  ${c_dim}·${c_rst} $name ${c_dim}(not set)${c_rst}"; fi
    done
    [[ $any -eq 0 ]] && echo "${c_ylw}No keys yet — run: bash setup_keys.sh${c_rst}"
}

cmd_check() {
    echo "${c_bld}Validating stored keys against live APIs...${c_rst}"
    local spec name code v any=0
    for spec in "${PROVIDERS[@]}"; do
        IFS='|' read -r name _ _ _ <<< "$spec"
        v=$(store_get "$name")
        [[ -z "$v" ]] && continue
        any=1
        code=$(validate_key "$name" "$v"; true)
        if [[ $? -eq 0 ]]; then echo "  ${c_grn}✓${c_rst} $name $(mask "$v") — ${c_grn}VALID${c_rst}"
        else echo "  ${c_red}✗${c_rst} $name $(mask "$v") — ${c_red}rejected (HTTP $code)${c_rst}"; fi
    done
    [[ $any -eq 0 ]] && echo "${c_ylw}No keys stored. Run: bash setup_keys.sh${c_rst}"
}

cmd_interactive() {
    echo "${c_bld}Master AI — API key setup${c_rst}"
    echo "Keys are stored in ${c_bld}$KEYS_FILE${c_rst} (mode 0600, never uploaded anywhere)."
    echo "At least one cloud key OR a local Ollama install is required to run."
    echo "Press Enter to skip any provider.${c_rst}"
    echo
    local spec name display url v
    for spec in "${PROVIDERS[@]}"; do
        IFS='|' read -r name display url _ <<< "$spec"
        local existing; existing=$(store_get "$name")
        if [[ -n "$existing" ]]; then
            read -rp "  $display — already set ($(mask "$existing")). Replace? [y/N] " ans
            [[ "$ans" != "y" && "$ans" != "Y" ]] && continue
        fi
        read -rp "  Paste $display key (${c_dim}$url${c_rst}): " v
        v="${v//[$'\t\r\n ']/}"
        [[ -z "$v" ]] && { echo "    ${c_dim}skipped${c_rst}"; continue; }
        store_set "$name" "$v"
        code=$(validate_key "$name" "$v"; true)
        if [[ $? -eq 0 ]]; then echo "    ${c_grn}✓ valid${c_rst}"
        else echo "    ${c_ylw}⚠ saved but could not validate (HTTP $code) — check it later with --check${c_rst}"; fi
    done
    echo
    cmd_list
    echo
    echo "${c_bld}Done.${c_rst} Routing auto-detects these at startup — start with: ${c_bld}master-ai${c_rst}"
}

case "${1:-}" in
    --check) need_python; cmd_check ;;
    --list)  need_python; cmd_list ;;
    --remove)
        need_python
        [[ -n "${2:-}" ]] || { echo "usage: setup_keys.sh --remove <provider>" >&2; exit 1; }
        store_del "$2" ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//' ;;
    *) need_python; cmd_interactive ;;
esac
