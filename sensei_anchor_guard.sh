#!/usr/bin/env bash
# =============================================================================
# SENSEI ANCHOR GUARD — Pre-flight Enforcement Wrapper
# Version: 1.0
# Purpose: Zero-tolerance pre-checks before verify_agent.sh is allowed to run.
#          No TTY = no run. No screenshot tool = no run. No Chrome = no run.
#          This wrapper IS the dead-man's switch. If it fails, the test is void.
# =============================================================================

set -euo pipefail
IFS=$'\n\t'

# --- CONFIGURATION ---
TARGET_SCRIPT="${HOME}/scripts/verify_agent.sh"
EVIDENCE_DIR="${HOME}/sensei_verify_evidence"
MIN_TERMINAL_COLS=80
MIN_TERMINAL_ROWS=24
REQUIRED_SCREENSHOT_TOOLS=("scrot" "gnome-screenshot" "import")
CHROME_CHECK_URL="http://localhost:8080/extension/queue"
CLAF_HEALTH_URL="http://localhost:8000/healthz"
BRIDGE_HEALTH_URL="http://localhost:8080/extension/queue"

# --- COLOR CODES ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# --- UTILITY FUNCTIONS ---
die() {
    printf "${RED}${BOLD}ANCHOR GUARD FATAL:${NC} %s\n" "$1" >&2
    printf "${RED}Execution halted. Test is VOID. No phantom runs permitted.${NC}\n" >&2
    exit 1
}

warn() {
    printf "${YELLOW}${BOLD}ANCHOR GUARD WARNING:${NC} %s\n" "$1" >&2
}

pass() {
    printf "${GREEN}${BOLD}✓ ANCHOR GUARD PASS:${NC} %s\n" "$1"
}

banner() {
    [[ "${ANCHOR_NO_CLEAR:-0}" == "1" ]] || clear
    printf "\n${BLUE}${BOLD}"
    printf "╔══════════════════════════════════════════════════════════════════════════════╗\n"
    printf "║  SENSEI ANCHOR GUARD — Pre-Flight Enforcement                                ║\n"
    printf "║  Timestamp: %-66s ║\n" "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    printf "║  Operator: %-67s ║\n" "$(whoami)@$(hostname)"
    printf "╚══════════════════════════════════════════════════════════════════════════════╝\n"
    printf "${NC}\n"
}

# --- CHECK 1: TTY ENFORCEMENT (Display Surface Anchoring) ---
check_tty() {
    printf "${BOLD}CHECK 1: Display Surface Anchoring (TTY)${NC}\n"

    if [[ ! -t 0 ]] || [[ ! -t 1 ]] || [[ ! -t 2 ]]; then
        die "STDIN, STDOUT, or STDERR is not a TTY. \
This script MUST run in an interactive terminal with a human operator present. \
Launch with: bash ~/scripts/sensei_anchor_guard.sh \
(Not via pipe, cron, systemd, or background process.)"
    fi

    if [[ -z "${TERM:-}" ]] || [[ "${TERM}" == "dumb" ]]; then
        die "TERM is unset or 'dumb'. Terminal is not capable of visual rendering. \
Use a real terminal emulator (gnome-terminal, konsole, alacritty, etc.)."
    fi

    local cols rows
    cols=$(tput cols 2>/dev/null || echo 0)
    rows=$(tput lines 2>/dev/null || echo 0)

    if [[ "${cols}" -lt "${MIN_TERMINAL_COLS}" ]] || [[ "${rows}" -lt "${MIN_TERMINAL_ROWS}" ]]; then
        die "Terminal too small: ${cols}x${rows}. \
Minimum required: ${MIN_TERMINAL_COLS}x${MIN_TERMINAL_ROWS}. \
Maximize your terminal window. Visual anchoring requires adequate display real estate."
    fi

    pass "TTY confirmed. Terminal: ${cols}x${rows}. Operator physically present."
}

# --- CHECK 2: SCREENSHOT TOOL ENFORCEMENT (Evidence Anchoring) ---
check_screenshot_tool() {
    printf "\n${BOLD}CHECK 2: Screen Evidence Tool (Visual Audit Trail)${NC}\n"

    local found_tool=""
    for tool in "${REQUIRED_SCREENSHOT_TOOLS[@]}"; do
        if command -v "${tool}" &>/dev/null; then
            found_tool="${tool}"
            break
        fi
    done

    if [[ -z "${found_tool}" ]]; then
        die "No screenshot tool found. \
Required one of: ${REQUIRED_SCREENSHOT_TOOLS[*]}. \
Install with: sudo apt install scrot (or gnome-screenshot, or imagemagick). \
Without screenshot capability, there is no visual audit trail. Test is VOID."
    fi

    # Test the tool actually works
    local test_shot="${EVIDENCE_DIR}/.guard_test_$(date +%s).png"
    mkdir -p "${EVIDENCE_DIR}"

    case "${found_tool}" in
        scrot)
            scrot "${test_shot}" &>/dev/null || die "scrot installed but failed to capture test screenshot."
            ;;
        gnome-screenshot)
            gnome-screenshot -f "${test_shot}" &>/dev/null || die "gnome-screenshot installed but failed to capture."
            ;;
        import)
            import -window root "${test_shot}" &>/dev/null || die "ImageMagick import failed. Is X11/Wayland accessible?"
            ;;
    esac

    if [[ ! -f "${test_shot}" ]] || [[ ! -s "${test_shot}" ]]; then
        die "Screenshot tool (${found_tool}) produced empty or missing test file. \
Display server (X11/Wayland) may be inaccessible. Test is VOID."
    fi

    rm -f "${test_shot}"
    pass "Screenshot tool confirmed: ${found_tool}. Evidence directory writable: ${EVIDENCE_DIR}"
}

# --- CHECK 3: TARGET SCRIPT EXISTENCE ---
check_target_script() {
    printf "\n${BOLD}CHECK 3: Target Script Integrity${NC}\n"

    if [[ ! -f "${TARGET_SCRIPT}" ]]; then
        die "Target script not found: ${TARGET_SCRIPT}. \
Cannot verify what does not exist. Test is VOID."
    fi

    if [[ ! -x "${TARGET_SCRIPT}" ]]; then
        warn "Target script not executable. Fixing: chmod +x ${TARGET_SCRIPT}"
        chmod +x "${TARGET_SCRIPT}"
    fi

    # Syntax check
    if ! bash -n "${TARGET_SCRIPT}"; then
        die "Target script has syntax errors. Run 'bash -n ${TARGET_SCRIPT}' manually. Test is VOID."
    fi

    pass "Target script exists and is syntactically valid: ${TARGET_SCRIPT}"
}

# --- CHECK 4: EVIDENCE DIRECTORY ---
check_evidence_dir() {
    printf "\n${BOLD}CHECK 4: Evidence Directory (Non-Repudiable Storage)${NC}\n"

    mkdir -p "${EVIDENCE_DIR}"

    if [[ ! -d "${EVIDENCE_DIR}" ]]; then
        die "Cannot create evidence directory: ${EVIDENCE_DIR}. \
Disk full? Permission denied? Without evidence storage, there is no audit trail. Test is VOID."
    fi

    if [[ ! -w "${EVIDENCE_DIR}" ]]; then
        die "Evidence directory not writable: ${EVIDENCE_DIR}. \
Fix permissions or choose a different path. Test is VOID."
    fi

    # Check available space (minimum 100MB)
    local avail_kb
    avail_kb=$(df -k "${EVIDENCE_DIR}" | awk 'NR==2 {print $4}')
    if [[ "${avail_kb}" -lt 102400 ]]; then
        die "Evidence directory has < 100MB free space (${avail_kb}KB). \
10 screenshots + logs require space. Test is VOID."
    fi

    pass "Evidence directory ready: ${EVIDENCE_DIR} (${avail_kb}KB free)"
}

# --- CHECK 5: BACKEND SERVICES (Optional but Strict) ---
check_services() {
    printf "\n${BOLD}CHECK 5: Backend Service Visibility${NC}\n"

    local claf_up=false
    local bridge_up=false

    if curl -s --max-time 3 "${CLAF_HEALTH_URL}" &>/dev/null; then
        claf_up=true
        pass "CLAF health endpoint responding: ${CLAF_HEALTH_URL}"
    else
        warn "CLAF health endpoint NOT responding: ${CLAF_HEALTH_URL}. \
verify_agent.sh may attempt to start it, but you must witness this."
    fi

    if curl -s --max-time 3 "${BRIDGE_HEALTH_URL}" &>/dev/null; then
        bridge_up=true
        pass "Bridge queue endpoint responding: ${BRIDGE_HEALTH_URL}"
    else
        warn "Bridge queue endpoint NOT responding: ${BRIDGE_HEALTH_URL}. \
verify_agent.sh may attempt to start it. Witness the startup."
    fi

    if [[ "${claf_up}" == false ]] || [[ "${bridge_up}" == false ]]; then
        printf "\n${YELLOW}${BOLD}⚠ SERVICES NOT FULLY UP${NC}\n"
        printf "The target script will likely attempt to launch services. \
You must visually witness each service coming online. \
Do not attest to a service you did not see start.\n"
        read -r -p "Continue anyway? [y/N] " response
        if [[ ! "${response}" =~ ^[Yy]$ ]]; then
            die "Operator chose to halt. Correct decision when services are not visible."
        fi
    fi
}

# --- CHECK 6: CHROME / SIDE PANEL PRESENCE (Step 10 Pre-Check) ---
check_chrome_panel() {
    printf "\n${BOLD}CHECK 6: Chrome Side Panel Presence (Step 10 Pre-Flight)${NC}\n"

    # Check if Chrome/Chromium process is running
    local chrome_pid
    chrome_pid=$(pgrep -x "chrome" || pgrep -x "chromium" || pgrep -x "chromium-browser" || echo "")

    if [[ -z "${chrome_pid}" ]]; then
        warn "No Chrome/Chromium process detected. \
For Step 10 (LIVE BROWSER_READ), Chrome must be running with the SENSEI side panel open."
    else
        pass "Chrome process detected (PID: ${chrome_pid})."
    fi

    printf "\n${BOLD}${YELLOW}MANUAL CHECK REQUIRED:${NC}\n"
    printf "1. Is Chrome open on your screen RIGHT NOW? [y/N] "
    read -r response1
    if [[ ! "${response1}" =~ ^[Yy]$ ]]; then
        die "Chrome not confirmed open. Step 10 will fail or phantom. Open Chrome and re-run."
    fi

    printf "2. Is the SENSEI side panel VISIBLE and ACTIVE in Chrome? [y/N] "
    read -r response2
    if [[ ! "${response2}" =~ ^[Yy]$ ]]; then
        die "SENSEI side panel not confirmed active. Step 10 requires the panel. Activate it and re-run."
    fi

    pass "Operator attested: Chrome open + SENSEI panel active."
}

# --- CHECK 7: OPERATOR COGNITIVE STATE (No AFK, No Phantom) ---
check_operator_presence() {
    printf "\n${BOLD}CHECK 7: Operator Cognitive Attestation${NC}\n"

    printf "\n${RED}${BOLD}╔══════════════════════════════════════════════════════════════════════════════╗\n"
    printf "║  PHANTOM PREVENTION CONTRACT                                                  ║\n"
    printf "║                                                                              ║\n"
    printf "║  You are about to run a 10-step visually anchored verification.                ║\n"
    printf "║  You MUST be present at your keyboard for the entire duration.                 ║\n"
    printf "║  You MUST witness each step on your physical monitor.                        ║\n"
    printf "║  You MUST NOT type 'y' unless you SAW the result with your eyes.             ║\n"
    printf "║  Any step you did not see = 'n' = HALT = FAIL.                               ║\n"
    printf "║                                                                              ║\n"
    printf "║  A log file saying PASS is not proof. Your eyes are proof.                   ║\n"
    printf "╚══════════════════════════════════════════════════════════════════════════════╝\n"
    printf "${NC}\n"

    printf "Do you understand and agree to witness every step honestly? [y/N] "
    read -r response
    if [[ ! "${response}" =~ ^[Yy]$ ]]; then
        die "Operator declined the phantom prevention contract. Test is VOID. No hard feelings."
    fi

    pass "Operator contract signed. Witness integrity is now BINDING."
}

# --- MAIN EXECUTION ---
main() {
    banner

    printf "${BOLD}Running 7 pre-flight checks...${NC}\n"
    printf "Any failure = HALT. No exceptions. No workarounds. No phantom runs.\n\n"

    check_tty
    check_screenshot_tool
    check_target_script
    check_evidence_dir
    check_services
    check_chrome_panel
    check_operator_presence

    # --- ALL CHECKS PASSED ---
    printf "\n${GREEN}${BOLD}"
    printf "╔══════════════════════════════════════════════════════════════════════════════╗\n"
    printf "║  ALL 7 ANCHOR GUARD CHECKS PASSED                                            ║\n"
    printf "║  Launching verify_agent.sh in 5 seconds...                                   ║\n"
    printf "╚══════════════════════════════════════════════════════════════════════════════╝\n"
    printf "${NC}\n"

    sleep 5

    # Hand off to the target script with all arguments passed through
    exec "${TARGET_SCRIPT}" "$@"
}

# --- ENTRYPOINT ---
main "$@"
