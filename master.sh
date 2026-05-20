#!/usr/bin/env bash
# master.sh — Madam Mary portal (CLAF era)
# Usage: bash ~/scripts/master.sh

source ~/scripts/brand.sh 2>/dev/null || true

LOG_FILE="$HOME/scripts/master.log"
touch "$LOG_FILE" 2>/dev/null || true

log() {
    echo "[$(date '+%Y-%m-%d %I:%M:%S %p')] $1" >> "$LOG_FILE"
}

pause_read() {
    echo ""
    read -t 10 -rp "  [Enter to return — or wait 10s] " _ || true
}

# ── option functions ─────────────────────────────────────────

launch_madam() {
    log "--- madam launched ---"
    exec madam
}

health_check() {
    bash ~/scripts/ping_test.sh
}

fix_claf_stream() {
    echo -e "${C}  Running CLAF stream fix…${X}"
    bash ~/scripts/fix_claf_stream.sh
}

update_keys() {
    bash ~/scripts/update_keys.sh
}

claf_status() {
    echo -e "${C}  CLAF orchestrator status:${X}"
    curl -s http://localhost:8000/ && echo "" || echo -e "${R}  CLAF not responding on :8000${X}"
    echo ""
    echo -e "${C}  Sensei bridge status:${X}"
    curl -s http://localhost:8080/health && echo "" || echo -e "${R}  Bridge not responding on :8080${X}"
    echo ""
    echo -e "${C}  Ollama status:${X}"
    curl -s http://localhost:11434/api/tags | python3 -c "
import sys, json
try:
    tags = [m['name'] for m in json.load(sys.stdin)['models']]
    print('  models:', ', '.join(tags[:6]))
except:
    print('  (parse error)')
" 2>/dev/null || echo -e "${R}  Ollama not responding on :11434${X}"
}

view_howwework() {
    less ~/scripts/howwework.txt
}

# ── menu ─────────────────────────────────────────────────────

main_menu() {
    clear
    echo -e "${BG}  ╔═══════════════════════════════╗${X}"
    echo -e "${BG}  ║     Madam Mary — Portal       ║${X}"
    echo -e "${BG}  ╚═══════════════════════════════╝${X}"
    echo ""
    echo -e "  ${BC}1)${X} Launch madam  ${D}(Claude Code via CLAF)${X}"
    echo -e "  ${BC}2)${X} Health check  ${D}(ping_test.sh)${X}"
    echo -e "  ${BC}3)${X} Fix CLAF stream  ${D}(patch stream:true → false)${X}"
    echo -e "  ${BC}4)${X} Update keys  ${D}(API keys)${X}"
    echo -e "  ${BC}5)${X} CLAF / bridge status"
    echo -e "  ${BC}6)${X} How we work  ${D}(howwework.txt)${X}"
    echo ""
    echo -e "  ${D}x) exit${X}"
    echo ""
    printf "  Pick: "
    read -r CHOICE

    case "$CHOICE" in
        1) launch_madam ;;
        2) health_check; pause_read ;;
        3) fix_claf_stream; pause_read ;;
        4) update_keys ;;
        5) claf_status; pause_read ;;
        6) view_howwework ;;
        x|X) echo -e "${G}  Goodbye.${X}"; exit 0 ;;
        *) echo -e "${D}  Invalid option.${X}"; sleep 1 ;;
    esac

    main_menu
}

main_menu
