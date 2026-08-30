#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Master AI — API key setup (bash onboarding prompt)
#
# Paste any provider API key — it's auto-detected from its prefix and filed
# under the right name in ~/.master_ai_keys (JSON, chmod 600), the same file
# master_ai.py reads at startup via load_keys(). Routing (detect_route)
# auto-detects which providers you configured and picks the best lane:
#
#   no keys at all   -> local Ollama lane (CLAF local-first)
#   groq key         -> fast cloud lane (llama-3.3-70b)
#   openrouter key   -> deep/reasoning + fallback lane
#   gemini key       -> free-tier vision/cloud lane
#   fireworks key    -> cloud fallback lane
#   cerebras key     -> opt-in ultra-fast lane (`fast:` prefix)
#
# Usage:
#   bash setup_keys.sh                 interactive prompt (paste any key)
#   bash setup_keys.sh --check         verify stored keys against live APIs
#   bash setup_keys.sh --list          show which providers are configured
#   bash setup_keys.sh --remove NAME   delete one key from the store
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

KEYS_FILE="${MASTER_AI_KEYS_FILE:-$HOME/.master_ai_keys}"

# provider:display name:signup URL:validation method
# (signup URL / validation only used by --check and --list; detection below
#  is prefix-based so any of these — or an unlisted provider — can be pasted)
PROVIDERS=(
    "openrouter|OpenRouter (free + paid models, one key unlocks many)|https://openrouter.ai/settings/keys|bearer:https://openrouter.ai/api/v1/auth/key"
    "groq|Groq (free, fast)|https://console.groq.com/keys|bearer:https://api.groq.com/openai/v1/models"
    "gemini|Google Gemini (free tier)|https://aistudio.google.com/apikey|query:https://generativelanguage.googleapis.com/v1beta/models"
    "anthropic|Anthropic (paid — Claude)|https://console.anthropic.com/settings/keys|bearer:https://api.anthropic.com/v1/models"
    "openai|OpenAI (paid — gpt-4o)|https://platform.openai.com/api-keys|bearer:https://api.openai.com/v1/models"
    "fireworks|Fireworks AI|https://fireworks.ai/account/api-keys|bearer:https://api.fireworks.ai/inference/v1/models"
    "cerebras|Cerebras (ultra-fast)|https://cloud.cerebras.ai/platform|bearer:https://api.cerebras.ai/v1/models"
    "deepseek|DeepSeek (paid — R1 reasoning)|https://platform.deepseek.com/api_keys|bearer:https://api.deepseek.com/v1/models"
    "huggingface|HuggingFace|https://huggingface.co/settings/tokens|bearer:https://huggingface.co/api/whoami-v2"
    "xai|xAI (Grok)|https://console.x.ai|bearer:https://api.x.ai/v1/models"
    "nvidia|NVIDIA NIM (Llama/Nemotron catalog)|https://build.nvidia.com|bearer:https://integrate.api.nvidia.com/v1/models"
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

# KEYS_FILE is normally a symlink into the canonical KV keychain
# (~/Desktop/Projects/keychain/master_ai_keys, see KEYCHAIN.md) — KEY=VALUE,
# not JSON, and shared with other consumers (CLAF, keychain.sh). These
# helpers write through to the resolved target in KV form when that's what
# it already is, instead of replacing the symlink with a JSON blob.
_KV_MAP_PY='
_KV_KEY_MAP = {
    "OPENROUTER_API_KEY": "openrouter", "GROQ_API_KEY": "groq",
    "GEMINI_API_KEY": "gemini", "ANTHROPIC_CONSOLE_KEY": "anthropic",
    "CEREBRAS_API_KEY": "cerebras", "FIREWORKS_API_KEY": "fireworks",
    "OPENAI_API_KEY": "openai", "DEEPSEEK_API_KEY": "deepseek",
    "HUGGINGFACE_TOKEN": "huggingface", "HF_TOKEN": "huggingface",
    "NVIDIA_API_KEY": "nvidia",
}
_CANONICAL_NAME = {v: k for k, v in _KV_KEY_MAP.items() if k != "HF_TOKEN"}

def _resolve(path):
    return os.path.realpath(path) if os.path.islink(path) else path

def _load(target):
    if not os.path.exists(target):
        return {}, ""
    text = open(target).read()
    try:
        return json.loads(text), text
    except Exception:
        pass
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        short = _KV_KEY_MAP.get(name.strip())
        if short and val.strip():
            out[short] = val.strip()
    return out, text
'

store_set() {    # store_set <name> <value>
    need_python
    python3 - "$KEYS_FILE" "$1" "$2" <<PY
import json, os, sys
$_KV_MAP_PY
path, name, value = sys.argv[1], sys.argv[2], sys.argv[3]
target = _resolve(path)
data, text = _load(target)
canonical = _CANONICAL_NAME.get(name)
if canonical and text.strip() and not text.lstrip().startswith("{"):
    lines = text.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == canonical:
            lines[i] = f"{canonical}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{canonical}={value}")
    open(target, "w").write("\n".join(lines) + "\n")
else:
    data[name] = value
    open(target, "w").write(json.dumps(data, indent=2) + "\n")
os.chmod(target, 0o600)
print(f"  saved '{name}' -> {target} (0600)")
PY
}

store_del() {    # store_del <name>
    need_python
    python3 - "$KEYS_FILE" "$1" <<PY
import json, os, sys
$_KV_MAP_PY
path, name = sys.argv[1], sys.argv[2]
target = _resolve(path)
data, text = _load(target)
if name not in data:
    print(f"  '{name}' not present")
else:
    canonical = _CANONICAL_NAME.get(name)
    if canonical and text.strip() and not text.lstrip().startswith("{"):
        lines = [l for l in text.splitlines()
                 if not (l.strip() and not l.strip().startswith("#") and "=" in l.strip()
                         and l.strip().split("=", 1)[0].strip() == canonical)]
        open(target, "w").write("\n".join(lines) + "\n")
    else:
        del data[name]
        open(target, "w").write(json.dumps(data, indent=2) + "\n")
    os.chmod(target, 0o600)
    print(f"  removed '{name}'")
PY
}

store_get() {    # store_get <name> -> value or empty
    python3 - "$KEYS_FILE" "$1" 2>/dev/null <<PY || true
import json, os, sys
$_KV_MAP_PY
data, _ = _load(_resolve(sys.argv[1]))
v = data.get(sys.argv[2], "")
print(v if isinstance(v, str) else "")
PY
}

mask() {         # mask a key for display: first4****last4
    local v="$1"
    if [[ ${#v} -le 8 ]]; then echo "****"; else echo "${v:0:4}****${v: -4}"; fi
}

# detect_provider <key> -> provider name on stdout, or empty if unrecognized
detect_provider() {
    python3 - "$1" <<'PY'
import sys
key = sys.argv[1]
if key.startswith("gsk_"):        print("groq")
elif key.startswith("sk-ant-"):   print("anthropic")
elif key.startswith("sk-or-v1-"): print("openrouter")
elif key.startswith("sk-proj-"):  print("openai")
elif key.startswith("hf_"):       print("huggingface")
elif key.startswith("AIzaSy"):    print("gemini")
elif key.startswith("xai-"):      print("xai")
elif key.startswith("csk-"):      print("cerebras")
elif key.startswith("fw_"):       print("fireworks")
elif key.startswith("nvapi-"):    print("nvidia")
elif key.startswith("sk-"):       print("deepseek")
else: print("")
PY
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
    echo "Paste any provider key — groq, openrouter, gemini, anthropic, openai,"
    echo "fireworks, cerebras, deepseek, huggingface, xai, nvidia — it's auto-detected"
    echo "from its prefix and filed under the right name. Enter with nothing to finish.${c_rst}"
    echo
    local v provider
    while true; do
        read -rp "  Paste a key (or Enter to finish): " v
        v="${v//[$'\t\r\n ']/}"
        [[ -z "$v" ]] && break
        provider=$(detect_provider "$v")
        if [[ -z "$provider" ]]; then
            echo "    ${c_ylw}? couldn't identify this key's provider from its prefix — skipped${c_rst}"
            continue
        fi
        store_set "$provider" "$v"
        code=$(validate_key "$provider" "$v"; true)
        if [[ $? -eq 0 ]]; then echo "    ${c_grn}✓ $provider — valid${c_rst}"
        else echo "    ${c_ylw}⚠ $provider — saved but could not validate (HTTP $code) — check later with --check${c_rst}"; fi
        echo
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
