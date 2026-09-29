#!/bin/bash
# Master AI self-scan — reads the user's machine and recommends which
# models / modes will actually run comfortably on it.
#
# Parameters we settled on (2026-04-19):
#   Trifecta  : qwen2.5:3b (~1.9 GB) + qwen2.5:7b (~4.7 GB) + llava (~4.7 GB)
#   RAM floor : 8 GB   (spark only — 3b model, quick answers)
#   Recommend : 16 GB  (spark + brain; 7b daily driver)
#   Comfort   : 32 GB  (full trifecta including vision, 14B experiments)
#   Disk free : 15 GB  minimum for trifecta install
#   CPU       : x86_64 preferred; ARM via Ollama also supported
#   GPU       : not required (Elijah runs CPU-only on i7-6700T)
#
# Output tiers:
#   GREEN    — run the trifecta
#   YELLOW   — run spark + brain, skip vision until RAM upgrade
#   ORANGE   — spark only, upgrade recommended
#   RED      — insufficient resources, or missing prerequisites
#
# Usage: bash ~/scripts/selfscan.sh                 (one-shot scan)
#        bash ~/scripts/selfscan.sh --short         (one-line recommendation)
#        bash ~/scripts/selfscan.sh --post-install  (install.sh hand-off)

set -u
source ~/scripts/brand.sh 2>/dev/null || true

MODE="${1:-}"
SHORT=0; POST_INSTALL=0
case "$MODE" in
    --short)        SHORT=1 ;;
    --post-install) POST_INSTALL=1 ;;
esac

# ── collect facts ──────────────────────────────────────────────
hostname_v=$(hostname 2>/dev/null || echo unknown)
os_v=$(uname -s 2>/dev/null)
arch_v=$(uname -m 2>/dev/null)
kernel_v=$(uname -r 2>/dev/null)

# CPU
cpu_model=$(awk -F: '/model name/ {gsub(/^[ \t]+/,"",$2); print $2; exit}' /proc/cpuinfo 2>/dev/null)
cpu_cores=$(nproc 2>/dev/null || echo 1)

# RAM
mem_total_mb=$(awk '/MemTotal/     {print int($2/1024); exit}' /proc/meminfo 2>/dev/null)
mem_avail_mb=$(awk '/MemAvailable/ {print int($2/1024); exit}' /proc/meminfo 2>/dev/null)
swap_total_mb=$(awk '/SwapTotal/   {print int($2/1024); exit}' /proc/meminfo 2>/dev/null)

# Disk (~/)
disk_free_kb=$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')
disk_free_gb=$(awk -v k="${disk_free_kb:-0}" 'BEGIN {printf "%.1f", k/1024/1024}')

# GPU
gpu_info="none"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    gpu_info="NVIDIA $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
elif lspci 2>/dev/null | grep -qi 'vga.*amd\|display.*amd\|amdgpu'; then
    gpu_info="AMD (detected via lspci)"
elif lspci 2>/dev/null | grep -qi 'vga.*intel'; then
    gpu_info="Intel integrated (iGPU — CPU inference still preferred)"
fi

# Ollama
ollama_state="not installed"
ollama_version=""
if command -v ollama >/dev/null 2>&1; then
    ollama_state="installed"
    ollama_version=$(ollama --version 2>/dev/null | awk '{print $NF}')
    if curl -sf -m 2 http://localhost:11434/api/tags -o /tmp/selfscan_tags.json 2>/dev/null; then
        ollama_state="running"
    else
        ollama_state="installed (not running)"
    fi
fi
have_3b=0; have_7b=0; have_14b=0; have_llava=0
if [ -s /tmp/selfscan_tags.json ]; then
    grep -q '"qwen2.5:3b"'   /tmp/selfscan_tags.json && have_3b=1
    grep -q '"qwen2.5:7b"'   /tmp/selfscan_tags.json && have_7b=1
    grep -q '"qwen2.5:14b"'  /tmp/selfscan_tags.json && have_14b=1
    grep -q '"llava:latest"' /tmp/selfscan_tags.json && have_llava=1
fi
rm -f /tmp/selfscan_tags.json

# ── grade the box ───────────────────────────────────────────────
mem_gb=$(awk -v m="${mem_total_mb:-0}" 'BEGIN {printf "%.0f", m/1024}')

# Tier: green / yellow / orange / red
tier="red"
tier_color="${BR:-}"
reco_lines=()
missing_lines=()

# RAM tier
if   [ "${mem_total_mb:-0}" -ge 28000 ]; then
    tier="green"; tier_color="${BG:-}"
    reco_lines+=("RAM ${mem_gb} GB — full trifecta (3b spark + 7b brain + llava vision).")
    reco_lines+=("BIG BRAIN unlocked: add qwen2.5:14b (~9 GB) for real-work reasoning. Run: ollama pull qwen2.5:14b")
elif [ "${mem_total_mb:-0}" -ge 22000 ]; then
    tier="green"; tier_color="${BG:-}"
    reco_lines+=("RAM ${mem_gb} GB — trifecta runs clean. 14B big brain tier is available but tight — expect some swap under load.")
elif [ "${mem_total_mb:-0}" -ge 14000 ]; then
    tier="yellow"; tier_color="${BY:-}"
    reco_lines+=("RAM ${mem_gb} GB — spark + brain comfortable. Vision works but slow.")
    reco_lines+=("Upgrade to 32 GB to unlock the 14B big-brain tier — that's where local stops feeling small.")
elif [ "${mem_total_mb:-0}" -ge  7000 ]; then
    tier="orange"; tier_color="${BY:-}"
    reco_lines+=("RAM ${mem_gb} GB — run qwen2.5:3b (spark) as daily driver.")
    reco_lines+=("Brain (7b) will work but may swap under load. Skip vision for now.")
    reco_lines+=("Upgrade to 16 GB to unlock the 7b brain comfortably.")
else
    tier="red"; tier_color="${BR:-}"
    reco_lines+=("RAM ${mem_gb} GB — below floor. 3b spark may run; expect swapping.")
    reco_lines+=("Minimum recommended: 8 GB RAM.")
fi

# Disk guard
if [ "${disk_free_kb:-0}" -lt 5000000 ]; then
    missing_lines+=("Disk free ${disk_free_gb} GB — insufficient for trifecta install (need 15 GB).")
    [ "$tier" = "green" ] && { tier="red"; tier_color="${BR:-}"; }
elif [ "${disk_free_kb:-0}" -lt 15000000 ]; then
    reco_lines+=("Disk free ${disk_free_gb} GB — tight. Trifecta needs ~12 GB; leave 3 GB headroom.")
fi

# Ollama + model inventory
case "$ollama_state" in
    "not installed")
        # 2026-09-27: was "required" + tier="red". Red is a BLOCKER — in
        # POST_INSTALL mode red exits 1 and pauses the whole install, and
        # missing Ollama used to mean exactly that. Elijah: "make it to
        # where it's not so dependent upon the local model. make the local
        # model an option." No Ollama just means the free/private lane is
        # unavailable; the cloud lanes (and their free fallbacks) still run,
        # so this is yellow — degraded, not broken.
        missing_lines+=("Ollama not installed — OPTIONAL. Cloud lanes still work. Install for the free/private lane: https://ollama.com/download")
        [ "$tier" = "green" ] && { tier="yellow"; tier_color="${BY:-}"; }
        ;;
    "installed (not running)")
        missing_lines+=("Ollama installed but not running. Start: systemctl start ollama (or: ollama serve)")
        ;;
    "running")
        [ "$have_3b" = "0" ] && missing_lines+=("Model qwen2.5:3b not pulled — run: ollama pull qwen2.5:3b")
        [ "$have_7b" = "0" ] && missing_lines+=("Model qwen2.5:7b not pulled — run: ollama pull qwen2.5:7b (if RAM ≥ 16 GB)")
        [ "$have_llava" = "0" ] && [ "$tier" = "green" ] && missing_lines+=("Model llava not pulled — run: ollama pull llava (needed for vision)")
        ;;
esac

# Architecture note
case "$arch_v" in
    x86_64|amd64) ;;
    aarch64|arm64) reco_lines+=("ARM detected — Ollama supports it; CPU inference only.") ;;
    *) reco_lines+=("Unusual architecture ($arch_v) — test model compatibility before relying on it.") ;;
esac

# GPU note
case "$gpu_info" in
    NVIDIA*) reco_lines+=("GPU detected ($gpu_info) — Ollama may offload; expect faster tokens.") ;;
    AMD*)    reco_lines+=("AMD GPU detected — ROCm-on-Ollama is dicey; CPU inference is fine.") ;;
esac

# ── short mode: one-line output ───────────────────────────────
if [ "$SHORT" = "1" ]; then
    echo -e "${tier_color}tier: ${tier}${X:-}  ram: ${mem_gb}G  disk: ${disk_free_gb}G free  ollama: ${ollama_state}  models: 3b=${have_3b} 7b=${have_7b} 14b=${have_14b} llava=${have_llava}"
    exit 0
fi

# ── full report ───────────────────────────────────────────────
echo ""
echo -e "${BC:-}╔══════════════════════════════════════════════════════╗${X:-}"
echo -e "${BC:-}║${X:-}  ${BW:-}🥷 MASTER AI SELF-SCAN${X:-}                              ${BC:-}║${X:-}"
echo -e "${BC:-}║${X:-}  ${D:-}reads your box · recommends what to run · pre-install${X:-} ${BC:-}║${X:-}"
echo -e "${BC:-}╚══════════════════════════════════════════════════════╝${X:-}"
echo ""
echo -e "  ${BW:-}machine${X:-}    ${hostname_v}   (${os_v} ${arch_v}, kernel ${kernel_v})"
echo -e "  ${BW:-}cpu${X:-}        ${cpu_model:-unknown}   (${cpu_cores} core(s))"
echo -e "  ${BW:-}ram${X:-}        ${mem_total_mb:-0} MB total · ${mem_avail_mb:-0} MB available · swap ${swap_total_mb:-0} MB"
echo -e "  ${BW:-}disk free${X:-}  ${disk_free_gb} GB on ${HOME}"
echo -e "  ${BW:-}gpu${X:-}        ${gpu_info}"
echo -e "  ${BW:-}ollama${X:-}     ${ollama_state}${ollama_version:+ (v${ollama_version})}"
echo -e "  ${BW:-}models${X:-}     spark(3b)=$([ "$have_3b" = "1" ] && echo ✓ || echo ✗)  brain(7b)=$([ "$have_7b" = "1" ] && echo ✓ || echo ✗)  big(14b)=$([ "$have_14b" = "1" ] && echo ✓ || echo ✗)  vision(llava)=$([ "$have_llava" = "1" ] && echo ✓ || echo ✗)"
echo ""

# Tier banner
case "$tier" in
    green)  echo -e "  ${BG:-}🟢 GREEN — ready for the full trifecta.${X:-}" ;;
    yellow) echo -e "  ${BY:-}🟡 YELLOW — solid for spark + brain; vision will be slow.${X:-}" ;;
    orange) echo -e "  ${BY:-}🟠 ORANGE — spark-only today; upgrade path is clear.${X:-}" ;;
    red)    echo -e "  ${BR:-}🔴 RED — address blockers before installing.${X:-}" ;;
esac
echo -e "${X:-}"

if [ "${#reco_lines[@]}" -gt 0 ]; then
    echo -e "  ${BW:-}recommendation${X:-}"
    for r in "${reco_lines[@]}"; do echo "    · $r"; done
    echo ""
fi
if [ "${#missing_lines[@]}" -gt 0 ]; then
    echo -e "  ${BW:-}needs your attention${X:-}"
    for m in "${missing_lines[@]}"; do echo -e "    ${BY:-}!${X:-} $m"; done
    echo ""
fi

# Hand-off when called from install.sh
if [ "$POST_INSTALL" = "1" ]; then
    case "$tier" in
        green|yellow) exit 0 ;;
        orange)       echo -e "  ${BY:-}Install proceeds — but expect slow runs. Upgrade path above.${X:-}"; exit 0 ;;
        red)          echo -e "  ${BR:-}Install paused — fix the red items, then re-run install.sh${X:-}"; exit 1 ;;
    esac
fi

exit 0
