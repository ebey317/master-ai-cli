#!/bin/bash
# ============================================================
# MASTER AI — INSTALLER (cross-platform: Linux / macOS / WSL)
# One command. Prompts in Sensei's 4-button format so buyers
# learn the style from the first screen.
#
# Run:  bash install.sh
# ============================================================

set -u
source "$(dirname "$0")/brand.sh" 2>/dev/null || source ~/scripts/brand.sh 2>/dev/null || true

# Color fallbacks
: "${BC:=$(tput bold 2>/dev/null; tput setaf 4 2>/dev/null)}"
: "${BG:=$(tput bold 2>/dev/null; tput setaf 2 2>/dev/null)}"
: "${BY:=$(tput bold 2>/dev/null; tput setaf 3 2>/dev/null)}"
: "${BR:=$(tput bold 2>/dev/null; tput setaf 1 2>/dev/null)}"
: "${BW:=$(tput bold 2>/dev/null; tput setaf 0 2>/dev/null)}"
: "${D:=$(tput setaf 8 2>/dev/null)}"
: "${X:=$(tput sgr0 2>/dev/null)}"

# 2026-09-28: this install.sh used to make Ollama required and treat API
# keys as optional. That is backwards for the hobbyist market: cloud keys
# make Sensei useful immediately, local models are an upgrade path later.
# The flow below puts OpenRouter first and Ollama last.

INSTALL_LOG="$HOME/.master_ai_install.log"
APPROVE_ALL=0
SCRIPT_SRC="$(cd "$(dirname "$0")" && pwd)"
TARGET="$HOME/scripts"
mkdir -p "$HOME/.master_ai_approved_components" 2>/dev/null
: > "$INSTALL_LOG"
log() { echo "[$(date '+%I:%M:%S %p')] $1" >> "$INSTALL_LOG"; }

# ── 0. Early dependency checks (cheap, before prompts) ───────────
_need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo -e "  ${BR}✗ required command not found:${X} ${BW}$1${X}"; return 1; }; }
_missing_basic=0
for _cmd in bash python3 curl; do
    _need_cmd "$_cmd" || _missing_basic=1
done
[ "$_missing_basic" = "1" ] && { echo -e "  ${BR}Cannot continue without bash, python3, and curl.${X}"; exit 1; }

# ── OS detection ─────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Linux*)
            if grep -qi microsoft /proc/version 2>/dev/null; then echo "wsl"
            else echo "linux"; fi ;;
        Darwin*) echo "mac" ;;
        MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
        *) echo "unknown" ;;
    esac
}
OS=$(detect_os)

# ── Sensei-style 4-button prompt ─────────────────────────────
ask_install() {
    local name="$1" desc="$2"
    if [ "${MASTER_AI_NONINTERACTIVE:-0}" = "1" ]; then
        REPLY_CHOICE="yes"
        echo -e "  ${BC}⚡ non-interactive mode:${X} ${BW}$name${X}"; return 0
    fi
    if [ "$APPROVE_ALL" = "1" ]; then
        REPLY_CHOICE="yes"
        echo -e "  ${BC}⚡ auto-approved (All mode):${X} ${BW}$name${X}"; return 0
    fi
    local key
    key=$(echo "$name" | tr -c 'A-Za-z0-9' '_')
    if [ -f "$HOME/.master_ai_approved_components/$key" ]; then
        REPLY_CHOICE="yes"
        echo -e "  ${BC}⚡ previously approved:${X} ${BW}$name${X}"; return 0
    fi
    echo ""
    echo -e "${D}╔══════════════════════════════════════════════════════╗${X}"
    echo -e "${D}║  🥷 ${BW}Installer wants to add:${X}"
    echo -e "${D}║     ${BY}$name${X}"
    echo -e "${D}║${X}"
    echo -e "${D}║  ${D}$desc${X}"
    echo -e "${D}╠══════════════════════════════════════════════════════╣${X}"
    echo -e "${D}║${X}  ${BG} 1) Yes${X}      — install this one"
    echo -e "${D}║${X}  ${BC} 2) Always${X}   — remember this answer for next time"
    echo -e "${D}║${X}  ${BC} 3) All${X}      — yes to every remaining component"
    echo -e "${D}║${X}  ${BR} 4) No${X}       — skip this one (with instructions)"
    echo -e "${D}╚══════════════════════════════════════════════════════╝${X}"
    read -rp "  Choose (1/2/3/4): " choice
    case "$choice" in
        1) REPLY_CHOICE="yes" ;;
        2) REPLY_CHOICE="yes"; touch "$HOME/.master_ai_approved_components/$key"
           echo -e "  ${BG}✅ remembered — won't ask for '$name' again${X}" ;;
        3) REPLY_CHOICE="yes"; APPROVE_ALL=1
           echo -e "  ${BG}✅ All mode on — every remaining component auto-approved${X}" ;;
        4|*) REPLY_CHOICE="no" ;;
    esac
}

# Non-interactive guard: skip the final auto-launch prompt too
noninteractive_skip_prompt() {
    if [ "${MASTER_AI_NONINTERACTIVE:-0}" = "1" ]; then
        echo -e "  ${D}  (non-interactive: skipping launch prompt)${X}"
        return 0
    fi
    return 1
}

# ── 1. banner + intro ────────────────────────────────────────
clear
echo ""
echo -e "  ${BC}╔══════════════════════════════════════════════════════╗${X}"
echo -e "  ${BC}║${X}  ${BW}🥷  MASTER AI — INSTALLER${X}                         ${BC}║${X}"
echo -e "  ${BC}║${X}  ${D}your AI, on your hardware${X}                         ${BC}║${X}"
echo -e "  ${BC}╚══════════════════════════════════════════════════════╝${X}"
echo ""
echo -e "  ${BW}Detected system:${X} $OS"
case "$OS" in
    linux)   echo -e "  ${BG}✓ native Linux — full install supported${X}" ;;
    mac)     echo -e "  ${BG}✓ macOS — uses Homebrew + launchd instead of systemd${X}" ;;
    wsl)     echo -e "  ${BG}✓ WSL (Windows Subsystem for Linux) — full install supported${X}"
             echo -e "  ${D}  Note: systemd services only start if you enabled systemd in /etc/wsl.conf${X}" ;;
    windows) echo -e "  ${BR}⚠ Native Windows (Git Bash / MSYS) — partial support.${X}"
             echo -e "  ${BY}  Recommended: install WSL2, then run this installer inside Ubuntu.${X}"
             echo -e "  ${D}    PowerShell:  wsl --install${X}"
             echo -e "  ${D}    Then open Ubuntu and re-run this script there.${X}"
             read -rp "  Continue anyway (Windows-native)? (y/N) " w; [[ ! "$w" =~ ^[yY]$ ]] && exit 0 ;;
    *)       echo -e "  ${BR}? unknown OS — your mileage may vary${X}" ;;
esac
echo ""
echo -e "  ${BW}This installer sets up:${X}"
echo -e "    ${C}·${X} Sensei CLI & runtime"
echo -e "    ${C}·${X} Cloud model key (OpenRouter — required for Sensei to work)"
echo -e "    ${C}·${X} Optional local models (Ollama — more models for your money later)"
echo -e "    ${C}·${X} TTS voice (Piper — optional)"
echo -e "    ${C}·${X} Auto-start services (platform-appropriate)"
echo ""
echo -e "  ${BY}⚠ OpenRouter is required up front.${X}"
echo -e "  ${BW}Format you'll see:${X} ${BC}Yes / Always / All / No${X} (matches Sensei)"
echo ""
if [ "${MASTER_AI_NONINTERACTIVE:-0}" != "1" ]; then
    read -rp "  press Enter to begin (or Ctrl-C to cancel) " _
fi

# ── 2. Bootstrap: copy files to ~/scripts if not already there ───
echo ""
echo -e "  ${BC}━━━ STEP 1/6: install files ━━━${X}"
if [ "$SCRIPT_SRC" = "$TARGET" ]; then
    echo -e "  ${BG}✓ already installed at $TARGET — skipping copy${X}"
else
    ask_install "Install files to $TARGET" \
        "Copies Master AI's scripts + docs from this bundle into $TARGET so all commands work from anywhere. Won't overwrite existing $TARGET without confirmation."
    if [ "$REPLY_CHOICE" = "yes" ]; then
        if [ -d "$TARGET" ]; then
            echo -e "  ${BY}⚠ $TARGET exists — files will be copied over the top (your data preserved).${X}"
        fi
        mkdir -p "$TARGET"
        # Copy everything except the installer itself + .git + logs
        rsync -a --exclude="install.sh" --exclude=".git" --exclude="*.log" \
              "$SCRIPT_SRC/" "$TARGET/" 2>/dev/null \
          || cp -R "$SCRIPT_SRC/." "$TARGET/"
        # Also copy install.sh (useful for re-running)
        cp "$SCRIPT_SRC/install.sh" "$TARGET/install.sh" 2>/dev/null || true
        chmod +x "$TARGET"/*.sh 2>/dev/null
        echo -e "  ${BG}✓ files copied to $TARGET${X}"
        log "files copied from $SCRIPT_SRC to $TARGET"
    else
        echo -e "  ${BR}❌ cannot continue without files installed${X}"; exit 1
    fi
fi

# ── 2b. Required runtime directories ───────────────────────────
mkdir -p "$HOME/.master_ai_profiles/default" \
         "$HOME/.master_ai_skills" \
         "$HOME/.master_ai_mcp" \
         "$HOME/.master_ai_logs" \
         "$HOME/.master_ai_skins"
# Default profile seed (keeps the very first launch from erroring on missing dirs)
[ -f "$HOME/.master_ai_profiles/default/config.json" ] || cat > "$HOME/.master_ai_profiles/default/config.json" <<'EOF'
{"name": "default", "voice": "joe", "auto_mode": false}
EOF

# ── 3. API keys (OpenRouter required, others optional) ───────────
echo ""
echo -e "  ${BC}━━━ STEP 2/6: Cloud model key (REQUIRED) ━━━${X}"
echo ""
echo -e "  ${BW}Sensei needs a cloud model key to do anything useful.${X}"
echo -e "  ${D}OpenRouter is recommended: one key, many models, pay-as-you-go.${X}"
echo ""
echo -e "  ${BG}1)${X} ${BW}OpenRouter${X}        ${C}https://openrouter.ai/keys${X}"
echo -e "  ${BG}2)${X} ${BW}Groq${X}              ${C}https://console.groq.com/keys${X}"
echo -e "  ${BG}3)${X} ${BW}Google Gemini${X}     ${C}https://aistudio.google.com/app/apikey${X}"
echo -e "  ${BG}4)${X} ${BW}Anthropic Claude${X}  ${C}https://console.anthropic.com/settings/keys${X}"
echo ""

_openrouter_set=0
_key_attempts=0
while [ "$_openrouter_set" = "0" ] && [ "${MASTER_AI_NONINTERACTIVE:-0}" != "1" ]; do
    read -rp "  Paste an OpenRouter key (sk-or-v1-...) now, or Enter to skip: " key
    if [ -z "$key" ]; then
        if [ "$_key_attempts" -gt 0 ]; then
            echo -e "  ${BY}⚠ continuing without OpenRouter — Sensei won't answer until you add one later.${X}"
            break
        fi
        echo -e "  ${BY}⚠ OpenRouter is strongly recommended. You can add it later in ~/.master_ai_keys${X}"
        _key_attempts=$((_key_attempts + 1))
        continue
    fi
    python3 - "$key" <<'PY'
import json, os, sys
key = sys.argv[1]
field = None
if key.startswith("gsk_"):       field = "groq"
elif key.startswith("sk-ant-"):  field = "anthropic"
elif key.startswith("sk-or-v1-"):field = "openrouter"
elif key.startswith("sk-proj-"): field = "openai"
elif key.startswith("sk-"):      field = "deepseek"
elif key.startswith("hf_"):      field = "huggingface"
elif key.startswith("AIzaSy"):   field = "gemini"
elif key.startswith("xai-"):     field = "xai"
elif key.startswith("nvapi-"):   field = "nvidia"
if not field:
    print("  ? couldn't identify key prefix — skipped"); sys.exit(0)
path = os.path.expanduser("~/.master_ai_keys")
try: d = json.load(open(path))
except Exception: d = {}
if field in d and d[field] and d[field] != key:
    d[field + "_2"] = key; print(f"  ✅ {field}: saved as SECONDARY")
else:
    d[field] = key; print(f"  ✅ {field}: saved as PRIMARY")
with open(path, "w") as f: json.dump(d, f, indent=2)
os.chmod(path, 0o600)
PY
    if [ -n "$key" ] && [[ "$key" == sk-or-v1-* ]]; then
        _openrouter_set=1
    fi
    echo ""
    read -rp "  Paste another key, or Enter to finish: " key
    if [ -n "$key" ]; then
        python3 - "$key" <<'PY'
import json, os, sys
key = sys.argv[1]
field = None
if key.startswith("gsk_"):       field = "groq"
elif key.startswith("sk-ant-"):  field = "anthropic"
elif key.startswith("sk-or-v1-"):field = "openrouter"
elif key.startswith("sk-proj-"): field = "openai"
elif key.startswith("sk-"):      field = "deepseek"
elif key.startswith("hf_"):      field = "huggingface"
elif key.startswith("AIzaSy"):   field = "gemini"
elif key.startswith("xai-"):     field = "xai"
elif key.startswith("nvapi-"):   field = "nvidia"
if not field:
    print("  ? couldn't identify key prefix — skipped"); sys.exit(0)
path = os.path.expanduser("~/.master_ai_keys")
try: d = json.load(open(path))
except Exception: d = {}
if field in d and d[field] and d[field] != key:
    d[field + "_2"] = key; print(f"  ✅ {field}: saved as SECONDARY")
else:
    d[field] = key; print(f"  ✅ {field}: saved as PRIMARY")
with open(path, "w") as f: json.dump(d, f, indent=2)
os.chmod(path, 0o600)
PY
    fi
done

# ── 4. Ollama (OPTIONAL — more models for your money later) ────
echo ""
echo -e "  ${BC}━━━ STEP 3/6: Local models (optional) ━━━${X}"
echo -e "  ${BW}Ollama runs models locally. Optional.${X} ${D}You can skip this and use cloud only.${X}"
ask_install "Ollama (local model runtime)" \
    "Installs Ollama so you can run local models later. Cloud-only works fine without it."
if [ "$REPLY_CHOICE" = "yes" ]; then
    case "$OS" in
        linux|wsl)
            echo -e "  ${C}  Installing Ollama via official script...${X}"
            curl -fsSL https://ollama.com/install.sh | sh
            ;;
        mac)
            if command -v brew >/dev/null 2>&1; then
                echo -e "  ${C}  Installing Ollama via Homebrew...${X}"
                brew install ollama
            else
                echo -e "  ${BY}⚠ Homebrew not found. Skipping Ollama.${X}"
                echo -e "  ${D}   Download later from https://ollama.com/download${X}"
            fi
            ;;
        windows)
            echo -e "  ${BY}⚠ Ollama on native Windows — install later from:${X}"
            echo -e "  ${C}   https://ollama.com/download${X}"
            ;;
    esac
    log "Ollama installed"
fi

# Start the Ollama daemon if installed
case "$OS" in
    linux|wsl)
        if command -v ollama >/dev/null 2>&1 && ! systemctl is-active --quiet ollama 2>/dev/null; then
            systemctl start ollama 2>/dev/null || (nohup ollama serve >/tmp/ollama.log 2>&1 &)
            sleep 2
        fi ;;
    mac)
        if command -v ollama >/dev/null 2>&1 && ! pgrep -x ollama >/dev/null 2>&1; then
            echo -e "  ${C}  Starting Ollama in background...${X}"
            nohup ollama serve >/tmp/ollama.log 2>&1 &
            sleep 2
        fi ;;
esac
if command -v ollama >/dev/null 2>&1; then
    echo -e "  ${BG}✅ Ollama running at http://localhost:11434${X}"
fi

# ── 5. Models (only if Ollama is present) ──────────────────────
echo ""
echo -e "  ${BC}━━━ STEP 4/6: AI models (optional, Ollama only) ━━━${X}"
if ! command -v ollama >/dev/null 2>&1; then
    echo -e "  ${D}  Ollama not installed — skipping local model download.${X}"
else
    _ram_mb=$(awk '/MemTotal/ {print int($2/1024); exit}' /proc/meminfo 2>/dev/null || echo 16000)
    if [ "${_ram_mb:-0}" -ge 16000 ]; then
        MODELS=("qwen2.5:3b" "qwen2.5:7b" "llava:latest")
    elif [ "${_ram_mb:-0}" -ge 8000 ]; then
        MODELS=("qwen2.5:3b" "llava:latest")
        echo -e "  ${D}  (under 16 GB RAM detected — skipping qwen2.5:7b)${X}"
    else
        MODELS=("qwen2.5:3b")
        echo -e "  ${D}  (under 8 GB RAM detected — offering only the smallest model)${X}"
    fi
    for m in "${MODELS[@]}"; do
        if [ "${SKIP_MODELS:-0}" = "1" ]; then
            echo -e "  ${D}  SKIP_MODELS set — not checking $m${X}"; continue
        fi
        if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -q "^${m}\$"; then
            echo -e "  ${BG}✅ already have:${X} $m"; continue
        fi
        ask_install "Model: $m" \
            "$(case "$m" in
                qwen2.5:3b)       echo 'Spark — 3B model, ~1.9 GB, near-instant responses. Briefings, quick answers, idle thoughts. Start here if RAM is tight.';;
                qwen2.5:7b)       echo 'Brain — 7B model, ~4.7 GB, the daily driver. Code, chat, reasoning. Needs 16 GB+ RAM to run comfortably.';;
                llava:latest)     echo 'Eyes — vision + text in one model, ~4.7 GB. Required for the scrap scanner and apothecary. Skip if you are tight on disk.';;
            esac)"
        if [ "$REPLY_CHOICE" = "yes" ]; then
            echo -e "  ${C}  pulling $m — this takes a while...${X}"
            ollama pull "$m" && log "pulled $m"
        else
            echo -e "  ${BY}⚠ skipped $m${X} — run later: ${BW}ollama pull $m${X}"
        fi
    done

    # OPTIONAL: 14B big-brain tier (only pitched on 24 GB+ boxes)
    if [ "${SKIP_MODELS:-0}" != "1" ]; then
        ram_total_mb=$(awk '/MemTotal/ {print int($2/1024); exit}' /proc/meminfo 2>/dev/null)
        if [ "${ram_total_mb:-0}" -ge 24000 ]; then
            if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -q "^qwen2.5:14b\$"; then
                echo -e "  ${BG}✅ already have:${X} qwen2.5:14b (big brain)"
            else
                ask_install "Big brain: qwen2.5:14b (~9 GB, optional)" \
                    "You have enough RAM for the 14B tier. Pulls a model that keeps Sensei sharp on real work. Skip if you're low on disk."
                if [ "$REPLY_CHOICE" = "yes" ]; then
                    echo -e "  ${C}  pulling qwen2.5:14b — this takes a while (it's big)...${X}"
                    ollama pull qwen2.5:14b && log "pulled qwen2.5:14b (big brain)"
                else
                    echo -e "  ${BY}⚠ skipped big brain${X} — run later: ${BW}ollama pull qwen2.5:14b${X}"
                fi
            fi
        else
            echo -e "  ${D}  (big-brain 14B skipped — needs 24+ GB RAM; you have ~$((ram_total_mb/1024)) GB)${X}"
        fi
    fi
fi

# ── 6. TTS ─────────────────────────────────────────────────────
echo ""
echo -e "  ${BC}━━━ STEP 5/6: TTS (voice) ━━━${X}"
ask_install "TTS server (Piper voice synthesis)" \
    "Lets Sensei read pages aloud + gives it a voice. ~70 MB. Optional but recommended."
[ "$REPLY_CHOICE" = "yes" ] && {
    [ -f "$TARGET/tts_server.py" ] \
      && echo -e "  ${BG}✅ tts_server.py ready${X}" \
      || echo -e "  ${BY}⚠ tts_server.py missing — TTS will fall back to browser voice${X}"
} || echo -e "  ${BY}⚠ TTS skipped${X}"

# ── 7. Auto-start services ───────────────────────────────────
echo ""
echo -e "  ${BC}━━━ STEP 6/6: Auto-start services ━━━${X}"
ask_install "Auto-start on boot" \
    "Starts Pupil backend (:8080), TTS (:5050), and model keep-alive automatically on login. Runs 24/7. The 'always-on AI' experience."

if [ "$REPLY_CHOICE" = "yes" ]; then
    case "$OS" in
        linux|wsl)
            UD="$HOME/.config/systemd/user"
            mkdir -p "$UD"
            if [ -d "$TARGET/systemd" ]; then
                cp "$TARGET/systemd/"*.service "$UD/" 2>/dev/null
                # .path/.timer units ship too -- sensei-notify-drain needs both
                # (event trigger for latency, timer sweep so a missed trigger
                # can't strand a queued notification).
                cp "$TARGET/systemd/"*.path "$UD/" 2>/dev/null
                cp "$TARGET/systemd/"*.timer "$UD/" 2>/dev/null
                systemctl --user daemon-reload 2>/dev/null
                for u in sensei-notify-drain.path sensei-notify-drain.timer; do
                    [ -f "$UD/$u" ] && systemctl --user enable --now "$u" 2>/dev/null \
                      && echo -e "  ${BG}\u2713 $u enabled${X}"
                done
                for svc in master-ai-ui.service master-ai-tts.service master-ai-prewarm.service; do
                    [ -f "$UD/$svc" ] && systemctl --user enable --now "$svc" 2>/dev/null \
                      && echo -e "  ${BG}✓ $svc enabled${X}"
                done
                # Restart any already-enabled services so they pick up code changes
                for svc in master-ai-ui.service master-ai-tts.service master-ai-prewarm.service; do
                    if systemctl --user is-enabled "$svc" >/dev/null 2>&1; then
                        systemctl --user restart "$svc" 2>/dev/null \
                          && echo -e "  ${BG}✓ $svc restarted${X}" || echo -e "  ${BY}⚠ $svc restart failed${X}"
                    fi
                done
            else
                echo -e "  ${BY}⚠ $TARGET/systemd not found — service files not shipped in bundle${X}"
            fi ;;
        mac)
            AL="$HOME/Library/LaunchAgents"
            mkdir -p "$AL"
            cat > "$AL/com.master-ai.ui.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.master-ai.ui</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/env</string><string>python3</string>
    <string>$TARGET/stt_server.py</string><string>8080</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$TARGET/stt_server.log</string>
  <key>StandardErrorPath</key><string>$TARGET/stt_server.log</string>
</dict></plist>
EOF
            launchctl load "$AL/com.master-ai.ui.plist" 2>/dev/null \
              && echo -e "  ${BG}✓ LaunchAgent installed (com.master-ai.ui)${X}" ;;
        windows)
            echo -e "  ${BY}⚠ Native Windows auto-start not supported by this installer.${X}"
            echo -e "  ${D}   Workaround: create a shortcut to 'bash $TARGET/master.sh' in your Startup folder.${X}" ;;
    esac
else
    echo -e "  ${BY}⚠ auto-start skipped — run master.sh manually each time${X}"
fi

# ── 7. API keys ──────────────────────────────────────────────
# (moved to STEP 2/6 earlier in this file; this section is intentionally
# left minimal for upgrades that run an older layout — new installs hit the
# required-key prompt at STEP 2)
echo ""
echo -e "  ${BC}━━━ EXTRA: Additional cloud keys ━━━${X}"
echo ""
echo -e "  ${BW}Already set up OpenRouter above. Add fallbacks if you want:${X}"
echo ""
echo -e "  ${BG}1)${X} ${BW}Groq${X}              ${C}https://console.groq.com/keys${X}"
echo -e "  ${BG}2)${X} ${BW}Google Gemini${X}     ${C}https://aistudio.google.com/app/apikey${X}"
echo -e "  ${BG}3)${X} ${BW}HuggingFace${X}       ${C}https://huggingface.co/settings/tokens${X}"
echo -e "  ${BG}4)${X} ${BW}Anthropic Claude${X}  ${C}https://console.anthropic.com/settings/keys${X}"
echo ""
read -rp "  Paste a fallback key now (or Enter to skip): " key
while [ -n "$key" ] && [ "${MASTER_AI_NONINTERACTIVE:-0}" != "1" ]; do
    python3 - "$key" <<'PY'
import json, os, sys
key = sys.argv[1]
field = None
if key.startswith("gsk_"):       field = "groq"
elif key.startswith("sk-ant-"):  field = "anthropic"
elif key.startswith("sk-or-v1-"):field = "openrouter"
elif key.startswith("sk-proj-"): field = "openai"
elif key.startswith("sk-"):      field = "deepseek"
elif key.startswith("hf_"):      field = "huggingface"
elif key.startswith("AIzaSy"):   field = "gemini"
elif key.startswith("xai-"):     field = "xai"
elif key.startswith("nvapi-"):   field = "nvidia"
if not field:
    print("  ? couldn't identify key prefix — skipped"); sys.exit(0)
path = os.path.expanduser("~/.master_ai_keys")
try: d = json.load(open(path))
except Exception: d = {}
if field in d and d[field] and d[field] != key:
    d[field + "_2"] = key; print(f"  ✅ {field}: saved as SECONDARY")
else:
    d[field] = key; print(f"  ✅ {field}: saved as PRIMARY")
with open(path, "w") as f: json.dump(d, f, indent=2)
os.chmod(path, 0o600)
PY
    echo ""
    read -rp "  Paste another, or Enter to finish: " key
done

# ── 7b. Domain classifier list (Phase 1.5) ──────────────────
# Sensei's extension refuses to act on category 1/2 hosts. The list lives
# at ~/.master_ai_domain_classes.json and is refreshed weekly by the
# maintenance window (deep_clean.sh). On first install we seed it from
# the repo copy so the classifier has something to match against; the
# weekly refresh layers fresh URLhaus phishing/malware hosts on top.
DOMAIN_CLASSES_TARGET="$HOME/.master_ai_domain_classes.json"
DOMAIN_CLASSES_SEED="$TARGET/master_ai_domain_classes.seed.json"
if [ ! -f "$DOMAIN_CLASSES_TARGET" ] && [ -f "$DOMAIN_CLASSES_SEED" ]; then
    cp "$DOMAIN_CLASSES_SEED" "$DOMAIN_CLASSES_TARGET"
    chmod 644 "$DOMAIN_CLASSES_TARGET" 2>/dev/null || true
    echo -e "  ${BG}✓ Domain-classifier list seeded at${X} ${BW}~/.master_ai_domain_classes.json${X}"
fi
if [ -x "$TARGET/refresh_domain_classes.sh" ]; then
    echo -e "  ${BG}✓ Weekly refresh wired via deep_clean.sh${X} (manual: ${BW}bash ~/scripts/refresh_domain_classes.sh${X})"
fi

# ── 7c. Required runtime directories ───────────────────────────
mkdir -p "$HOME/.master_ai_profiles/default" \
         "$HOME/.master_ai_skills" \
         "$HOME/.master_ai_mcp" \
         "$HOME/.master_ai_logs" \
         "$HOME/.master_ai_skins"
# Default profile seed (keeps the very first launch from erroring on missing dirs)
[ -f "$HOME/.master_ai_profiles/default/config.json" ] || cat > "$HOME/.master_ai_profiles/default/config.json" <<'EOF'
{"name": "default", "voice": "joe", "auto_mode": false}
EOF

# ── 7d. Sandbox dependency check (Linux/WSL only) ────────────
if [ "$OS" = "linux" ] || [ "$OS" = "wsl" ]; then
    missing=0
    for dep in systemd-run unshare prlimit bash python3; do
        if ! command -v "$dep" >/dev/null 2>&1; then
            echo -e "  ${BR}✗ sandbox dependency missing:${X} ${BW}$dep${X}"; missing=1
        fi
    done
    if [ "$missing" = "0" ]; then
        echo -e "  ${BG}✓ sandbox dependencies present${X}"
    else
        echo -e "  ${BY}⚠ The RUN sandbox won't work without the missing dependencies above.${X}"
        echo -e "  ${D}   Install on Debian/Ubuntu: sudo apt-get install util-linux systemd bash python3${X}"
    fi
fi

# ── 8. Entry mode ────────────────────────────────────────────
rm -f "$HOME/.dojo_gate_sealed" 2>/dev/null || true
echo ""
echo -e "  ${BG}✓ Sensei opens directly — Dojo is optional from Projects.${X}"

# ── 9. Command launchers ─────────────────────────────────────
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR" 2>/dev/null || true
cat > "$BIN_DIR/master" <<EOF
#!/bin/bash
exec bash "$TARGET/master.sh" "\$@"
EOF
cat > "$BIN_DIR/sensei" <<EOF
#!/bin/bash
exec bash "$TARGET/launch_master_ai.sh" "\$@"
EOF
chmod +x "$BIN_DIR/master" "$BIN_DIR/sensei" 2>/dev/null || true
echo ""
echo -e "  ${BG}✓ terminal commands installed:${X} ${BW}master${X} and ${BW}sensei${X}"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo -e "  ${BY}⚠ $BIN_DIR is not on PATH in this shell.${X}"
        PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
        ADDED_PATH=0
        for PROFILE in "$HOME/.bashrc" "$HOME/.profile" "$HOME/.zshrc"; do
            [ -e "$PROFILE" ] || touch "$PROFILE" 2>/dev/null || continue
            if ! grep -Fq '$HOME/.local/bin' "$PROFILE" 2>/dev/null; then
                {
                    echo ""
                    echo "# Master AI terminal commands"
                    echo "$PATH_LINE"
                } >> "$PROFILE" 2>/dev/null && ADDED_PATH=1
            fi
        done
        export PATH="$BIN_DIR:$PATH"
        if [ "$ADDED_PATH" = "1" ]; then
            echo -e "  ${BG}✓ added $BIN_DIR to your shell PATH for future terminals${X}"
            echo -e "  ${D}  Current installer session updated too.${X}"
        else
            echo -e "  ${BY}⚠ could not update shell profile automatically.${X}"
            echo -e "  ${D}  Add this manually: export PATH=\"\$HOME/.local/bin:\$PATH\"${X}"
        fi
        ;;
esac

# ── 10. Self-scan — what can this box run? ───────────────────
if [ -x "$TARGET/selfscan.sh" ]; then
    echo ""
    echo -e "  ${BC}━━━ running self-scan (reads your machine) ━━━${X}"
    bash "$TARGET/selfscan.sh" --post-install || true
fi

# ── 11. Done — auto-launch ───────────────────────────────────
echo ""
echo -e "  ${BC}╔══════════════════════════════════════════════════════╗${X}"
echo -e "  ${BC}║${X}  ${BG}🥷  INSTALL COMPLETE${X}                              ${BC}║${X}"
echo -e "  ${BC}╚══════════════════════════════════════════════════════╝${X}"
echo ""
echo -e "  ${BW}Next:${X}"
echo -e "    ${BG}·${X} First run:   ${BW}bash $TARGET/first_run.sh${X}  (cleanup + Google setup)"
echo -e "    ${BG}·${X} Menu:        ${BW}master${X}  (or bash $TARGET/master.sh)"
echo -e "    ${BG}·${X} Sensei:      ${BW}sensei${X}  (direct terminal agent)"
echo -e "    ${BG}·${X} Tour:        open ${BW}$TARGET/slideshow.html${X}"
echo -e "    ${BG}·${X} Manual:      ${BW}$TARGET/README_FOR_BUYER.md${X}"
echo -e "    ${BG}·${X} Re-scan box: ${BW}bash $TARGET/selfscan.sh${X}  (or menu 19)"
echo ""
echo -e "  ${D}Log: $INSTALL_LOG${X}"
echo ""

# Auto-launch: ask once, then open first_run.sh so they're not stranded
ask_install "Run first-run setup now" \
    "Starts Sensei with a safe cleanup + optional Google Workspace setup so you see immediate value."
if noninteractive_skip_prompt; then
    echo -e "  ${D}  non-interactive: not launching first_run.sh${X}"
elif [ "$REPLY_CHOICE" = "yes" ]; then
    exec bash "$TARGET/first_run.sh"
fi
