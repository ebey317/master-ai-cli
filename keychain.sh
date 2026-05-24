#!/usr/bin/env bash
# keychain — list / probe / edit ~/.master_ai_keys (the official cloud key registry).
# See ~/scripts/KEYCHAIN.md for the full schema and conventions.
#
# usage:
#   keychain list             show registered names with masked values
#   keychain probe [name]     one tiny API call per enabled key (or just one)
#   keychain edit             open keychain in $EDITOR
#   keychain backup           timestamped copy
#   keychain path             print the canonical path
#   keychain check            verify env separation (no ANTHROPIC_API_KEY in this shell)

set -uo pipefail

KEYS_FILE="$HOME/.master_ai_keys"

die() { echo "keychain: $*" >&2; exit 1; }

cmd_path() {
    echo "$KEYS_FILE"
    if [ -L "$KEYS_FILE" ]; then
        echo "  -> $(readlink -f "$KEYS_FILE")"
    fi
}

cmd_list() {
    [ -f "$KEYS_FILE" ] || die "$KEYS_FILE does not exist"
    echo "Keychain: $KEYS_FILE"
    if [ -L "$KEYS_FILE" ]; then
        local real
        real=$(readlink -f "$KEYS_FILE")
        echo "  -> $real"
        echo "Perms:    $(stat -c '%a %U:%G' "$real")  (real file)"
    else
        echo "Perms:    $(stat -c '%a %U:%G' "$KEYS_FILE")"
    fi
    echo
    awk -F= '
        /^#/ { next }
        /^[[:space:]]*$/ { next }
        NF >= 2 {
            name = $1
            val = $2
            for (i = 3; i <= NF; i++) val = val "=" $i
            n = length(val)
            if (n >= 12) {
                mask = substr(val, 1, 8) "..." substr(val, n - 3)
            } else {
                mask = "***"
            }
            printf "  %-28s = %s  (%d chars)\n", name, mask, n
        }
    ' "$KEYS_FILE"
}

# Probe one provider with the smallest possible call. NEVER prints the key.
# Returns one line: "<name> <STATUS> [detail]".
probe_one() {
    local name="$1"
    local val
    val=$(awk -F= -v k="$name" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$KEYS_FILE")
    if [ -z "$val" ]; then
        printf "  %-28s SKIP  (not set in keychain)\n" "$name"
        return
    fi

    python3 - "$name" "$val" <<'PY'
import sys, json, urllib.request, urllib.error, socket
name, val = sys.argv[1], sys.argv[2]

# One tiny auth-required request per provider. /models endpoints that don't
# need auth (OpenRouter) are skipped in favor of /auth/key which does.
# A User-Agent header avoids Cloudflare 1010 blocks (Groq).
UA = "keychain.sh/1.0 (+local)"
specs = {
    "ANTHROPIC_CONSOLE_KEY": {
        "url": "https://api.anthropic.com/v1/models",
        "headers": {"x-api-key": val, "anthropic-version": "2023-06-01", "User-Agent": UA},
        "method": "GET",
    },
    "OPENROUTER_API_KEY": {
        # /auth/key requires the bearer; /models does not (would always 200).
        "url": "https://openrouter.ai/api/v1/auth/key",
        "headers": {"Authorization": f"Bearer {val}", "User-Agent": UA},
        "method": "GET",
    },
    "GROQ_API_KEY": {
        "url": "https://api.groq.com/openai/v1/models",
        "headers": {"Authorization": f"Bearer {val}", "User-Agent": UA},
        "method": "GET",
    },
    "GEMINI_API_KEY": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "headers": {"Authorization": f"Bearer {val}", "User-Agent": UA},
        "method": "GET",
    },
    "CEREBRAS_API_KEY": {
        "url": "https://api.cerebras.ai/v1/models",
        "headers": {"Authorization": f"Bearer {val}", "User-Agent": UA},
        "method": "GET",
    },
    "FIREWORKS_API_KEY": {
        "url": "https://api.fireworks.ai/inference/v1/models",
        "headers": {"Authorization": f"Bearer {val}", "User-Agent": UA},
        "method": "GET",
    },
    "OPENAI_API_KEY": {
        "url": "https://api.openai.com/v1/models",
        "headers": {"Authorization": f"Bearer {val}", "User-Agent": UA},
        "method": "GET",
    },
    "DEEPSEEK_API_KEY": {
        "url": "https://api.deepseek.com/v1/models",
        "headers": {"Authorization": f"Bearer {val}", "User-Agent": UA},
        "method": "GET",
    },
}
spec = specs.get(name)
if not spec:
    print(f"  {name:28s} UNKNOWN  (no probe defined)")
    sys.exit(0)

try:
    req = urllib.request.Request(spec["url"], headers=spec["headers"], method=spec["method"])
    with urllib.request.urlopen(req, timeout=8) as r:
        data = r.read()
    j = {}
    try: j = json.loads(data)
    except Exception: pass
    n = 0
    if isinstance(j, dict):
        if isinstance(j.get("data"), list): n = len(j["data"])
        elif isinstance(j.get("models"), list): n = len(j["models"])
    print(f"  {name:28s} OK    ({n} models visible)")
except urllib.error.HTTPError as e:
    body = ""
    try: body = e.read().decode()[:120]
    except Exception: pass
    if e.code == 401: tag = "AUTH"
    elif e.code == 429: tag = "RATE"
    elif e.code == 403: tag = "FORBIDDEN"
    elif e.code == 400 and "credit" in body.lower(): tag = "NOFUNDS"
    else: tag = f"HTTP{e.code}"
    print(f"  {name:28s} {tag}  ({body[:80]})")
except (urllib.error.URLError, socket.timeout) as e:
    print(f"  {name:28s} NET   ({e})")
except Exception as e:
    print(f"  {name:28s} ERR   ({type(e).__name__}: {e})")
PY
}

cmd_probe() {
    [ -f "$KEYS_FILE" ] || die "$KEYS_FILE does not exist"
    local target="${1:-}"
    if [ -n "$target" ]; then
        # accept both lowercase short name (groq) and full env var name
        case "$target" in
            anthropic|console) target="ANTHROPIC_CONSOLE_KEY" ;;
            groq) target="GROQ_API_KEY" ;;
            gemini) target="GEMINI_API_KEY" ;;
            openrouter|or) target="OPENROUTER_API_KEY" ;;
            cerebras) target="CEREBRAS_API_KEY" ;;
            fireworks) target="FIREWORKS_API_KEY" ;;
            openai|gpt) target="OPENAI_API_KEY" ;;
            deepseek|ds) target="DEEPSEEK_API_KEY" ;;
        esac
        probe_one "$target"
        return
    fi
    echo "Probing all keys in $KEYS_FILE (one /models call each, no token spend):"
    for k in ANTHROPIC_CONSOLE_KEY OPENAI_API_KEY DEEPSEEK_API_KEY OPENROUTER_API_KEY GROQ_API_KEY GEMINI_API_KEY CEREBRAS_API_KEY FIREWORKS_API_KEY; do
        probe_one "$k"
    done
}

cmd_edit() {
    [ -f "$KEYS_FILE" ] || die "$KEYS_FILE does not exist"
    "${EDITOR:-nano}" "$KEYS_FILE"
    chmod 600 "$KEYS_FILE"
    echo "Edited. Restart CLAF to pick up changes: pkill -f 'python3 orchestrator.py' && python3 ~/projects/claf/orchestrator.py"
}

cmd_backup() {
    [ -f "$KEYS_FILE" ] || die "$KEYS_FILE does not exist"
    local stamp
    stamp=$(date +%Y%m%d_%H%M%S)
    local dst="${KEYS_FILE}.bak.${stamp}_manual"
    cp "$KEYS_FILE" "$dst"
    chmod 600 "$dst"
    echo "Backed up to $dst"
}

cmd_check() {
    local issues=0
    echo "Env separation check:"
    if env | grep -q '^ANTHROPIC_API_KEY='; then
        echo "  FAIL  ANTHROPIC_API_KEY is set in this shell — Claude Code would read this and bypass Max OAuth."
        echo "        Source the keychain ONLY in CLAF's process (not in interactive shells)."
        issues=$((issues+1))
    else
        echo "  OK    ANTHROPIC_API_KEY is NOT in this shell's env"
    fi
    if [ -f "$HOME/.claude/.credentials.json" ]; then
        echo "  OK    ~/.claude/.credentials.json exists (Claude Code's Max OAuth state)"
    else
        echo "  WARN  ~/.claude/.credentials.json missing — Claude Code may not be logged in to Max"
        issues=$((issues+1))
    fi
    if grep -q '^ANTHROPIC_API_KEY=' "$KEYS_FILE" 2>/dev/null; then
        echo "  WARN  $KEYS_FILE still uses ANTHROPIC_API_KEY (should be ANTHROPIC_CONSOLE_KEY)"
        echo "        Rename to harden separation. See ~/scripts/KEYCHAIN.md."
        issues=$((issues+1))
    elif grep -q '^ANTHROPIC_CONSOLE_KEY=' "$KEYS_FILE" 2>/dev/null; then
        echo "  OK    $KEYS_FILE uses ANTHROPIC_CONSOLE_KEY (correct name)"
    fi
    if grep -q 'unset ANTHROPIC_API_KEY' "$HOME/projects/claf/launch.sh" 2>/dev/null; then
        echo "  OK    launch.sh unsets ANTHROPIC_API_KEY before exec claude"
    else
        echo "  WARN  launch.sh does not unset ANTHROPIC_API_KEY"
        issues=$((issues+1))
    fi
    echo
    if [ "$issues" -eq 0 ]; then
        echo "All clean."
    else
        echo "$issues issue(s) found."
        exit 1
    fi
}

case "${1:-}" in
    list|ls)     cmd_list ;;
    probe|test)  shift; cmd_probe "${1:-}" ;;
    edit)        cmd_edit ;;
    backup)      cmd_backup ;;
    path)        cmd_path ;;
    check)       cmd_check ;;
    ""|-h|--help|help)
        cat <<USAGE
keychain — official cloud key registry tool

Canonical file: $KEYS_FILE
Docs:           ~/scripts/KEYCHAIN.md

usage:
  keychain list             show registered names with masked values
  keychain probe [name]     one tiny /models call per key (no token spend)
  keychain edit             open keychain in \$EDITOR (chmod 600 after)
  keychain backup           timestamped manual backup
  keychain path             print the canonical path
  keychain check            verify env separation (Max OAuth vs Console key)

Shortcut names for 'probe': anthropic, groq, gemini, openrouter, cerebras, fireworks
USAGE
        ;;
    *) die "unknown command '$1' — run 'keychain help'" ;;
esac
