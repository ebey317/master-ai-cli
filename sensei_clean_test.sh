#!/usr/bin/env bash
# Sensei Clean — interactive standalone test runner.
# Launched by ~/Desktop/SenseiClean.desktop (and runnable directly).
#
# Walks the user through scan -> review -> apply -> exit. Undo is shown
# as a separate command they can run later. Never moves files unless the
# user types y at the apply prompt.

set -uo pipefail

cd "$HOME"

cyan="\e[36m"
yellow="\e[33m"
green="\e[32m"
dim="\e[2m"
reset="\e[0m"

banner() {
  printf "${cyan}Sensei Clean — review-first local cleanup${reset}\n"
  printf "${dim}standalone test runner${reset}\n\n"
}

banner

printf "Choose what to scan:\n"
printf "  1) Demo (safe): scans only a temporary folder under /tmp with fake files\n"
printf "  2) Custom roots: you type folders to scan (advanced)\n\n"
read -rp "Selection [1]: " sel
sel="${sel:-1}"

# Try the PATH version first; fall back to the script if not installed.
if command -v sensei-clean >/dev/null 2>&1; then
  SC=sensei-clean
else
  SC="python3 $HOME/scripts/sensei_clean_cli.py"
fi

OUT=/tmp/sensei_clean_scan.out
RUN_DIR=""

if [[ "$sel" == "2" ]]; then
  printf "\nSCAN (custom roots)\n"
  printf "  Notes:\n"
  printf "  - Scanning is local-only, but it still reads file names and contents.\n"
  printf "  - For privacy, avoid roots like ~/Documents and ~/Pictures unless you explicitly intend that.\n\n"
  read -rp "Enter one or more roots (space-separated): " -a ROOTS
  if [[ "${#ROOTS[@]}" -lt 1 ]]; then
    printf "${yellow}no roots provided. Bail.${reset}\n"
    read -rp "  Enter to close: " _
    exit 1
  fi
  RUN_DIR="$(mktemp -d /tmp/sensei_clean_run_XXXXXX)"
  $SC scan --sha256 --run-dir "$RUN_DIR" --roots "${ROOTS[@]}" | tee "$OUT"
else
  printf "\nSCAN (demo / safe)\n"
  printf "  Reads: /tmp/sensei_clean_demo_<id>/ only\n"
  printf "  Writes: /tmp/sensei_clean_run_<id>/ only\n"
  printf "  Hashing is ON for the demo so duplicates are found deterministically.\n\n"
  read -rp "  Enter to run demo scan, Ctrl-C to abort: " _

  DEMO_ROOT="$(mktemp -d /tmp/sensei_clean_demo_XXXXXX)"
  mkdir -p "$DEMO_ROOT"/A "$DEMO_ROOT"/B "$DEMO_ROOT"/C
  printf "hello world\n" > "$DEMO_ROOT/A/dup.txt"
  cp "$DEMO_ROOT/A/dup.txt" "$DEMO_ROOT/B/dup.txt"
  printf "unique\n" > "$DEMO_ROOT/C/unique.txt"

  RUN_DIR="$(mktemp -d /tmp/sensei_clean_run_XXXXXX)"
  $SC scan --sha256 \
    --run-dir "$RUN_DIR" \
    --roots "$DEMO_ROOT" \
    --quarantine-root "$DEMO_ROOT/Quarantine" | tee "$OUT"
fi
echo

# Prefer the run dir printed by scan, but fall back to our mktemp dir.
PARSED_RUN_DIR="$(grep -m1 "^  run:" "$OUT" | awk '{print $2}')"
if [[ -n "${PARSED_RUN_DIR}" ]]; then
  RUN_DIR="${PARSED_RUN_DIR}"
fi
if [[ -z "${RUN_DIR}" || ! -d "${RUN_DIR}" ]]; then
  printf "${yellow}could not parse run dir from scan output. Bail.${reset}\n"
  read -rp "  Enter to close: " _
  exit 1
fi

printf "\n${cyan}Step 2: REVIEW${reset}\n"
printf "  Report: ${RUN_DIR}/reports/summary.md\n\n"
read -rp "  Open the report? [y/N]: " ans
case "${ans,,}" in
  y|yes)
    xdg-open "${RUN_DIR}/reports/summary.md" >/dev/null 2>&1 \
      || cat "${RUN_DIR}/reports/summary.md"
    echo
    ;;
esac

printf "\n${cyan}Step 3: APPLY${reset}\n"
printf "  Applies unattended-lane actions only by default.\n"
printf "  Monitored-lane (sensitive) items are skipped unless you opt in.\n\n"
read -rp "  Apply unattended-lane actions? [y/N]: " ans
case "${ans,,}" in
  y|yes)
    read -rp "  Also include monitored (sensitive) items? [y/N]: " mon
    if [[ "${mon,,}" == "y" || "${mon,,}" == "yes" ]]; then
      $SC apply "${RUN_DIR}" --yes --approve-monitored
    else
      $SC apply "${RUN_DIR}" --yes
    fi
    echo
    printf "${green}Apply complete. Journal: ${RUN_DIR}/undo.jsonl${reset}\n"
    ;;
  *)
    printf "${dim}skipped apply — nothing moved.${reset}\n"
    ;;
esac

printf "\n${cyan}Step 4: UNDO${reset}\n"
printf "  Reverse what apply did at any time with:\n"
printf "    sensei-clean undo ${RUN_DIR}\n\n"
read -rp "  Enter to close: " _
