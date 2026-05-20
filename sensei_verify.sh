#!/usr/bin/env bash
# sensei_verify.sh — DEPRECATED unattended verifier.
#
# The operator policy is:
#   "A result that was not visually observed and human-attested is
#    classified as hallucinated regardless of what any log file claims."
#
# This script used to print a structural ✓/✗ table without operator
# attestation. That is no longer the canonical verification path. The
# canonical path is:
#
#       bash ~/scripts/verify_agent.sh
#
# which gates every step on ENTER, prompts y/N attestation, and writes
# screenshot evidence to ~/sensei_verify_evidence/.
#
# This script remains callable for CI-style smoke checks ONLY when
# explicitly invoked with --unattended-no-witness, and in that mode
# every output line is stamped UNWITNESSED so no downstream consumer
# can mistake the result for an attested PASS.

set +e
set -u

UNATTENDED=0
SHOW_HELP=0
for arg in "$@"; do
    case "$arg" in
        --unattended-no-witness) UNATTENDED=1 ;;
        --read|--headless)
            # Legacy flags — explicitly route to verify_agent.sh dry-run.
            echo "Legacy flag $arg — delegating to verify_agent.sh --dry-run"
            exec bash "$HOME/scripts/verify_agent.sh" --dry-run
            ;;
        -h|--help) SHOW_HELP=1 ;;
        *)
            echo "unknown arg: $arg" >&2
            SHOW_HELP=1
            ;;
    esac
done

if [[ "$SHOW_HELP" == "1" || ( "$UNATTENDED" == "0" && $# -eq 0 ) ]]; then
    cat <<'EOM'
sensei_verify.sh — DEPRECATED unattended verifier.

The canonical, operator-attested verifier is:
    bash ~/scripts/verify_agent.sh

That runner gates every step on ENTER, prompts y/N for visual
attestation, writes screenshot evidence to ~/sensei_verify_evidence/,
and derives its verdict from witnessed state — not log parsing.

Modes:
    bash ~/scripts/verify_agent.sh             # full live, gated
    bash ~/scripts/verify_agent.sh --dry-run   # banners + gates only
    bash ~/scripts/sensei_verify.sh --unattended-no-witness
        # legacy structural smoke — every line stamped UNWITNESSED,
        # NOT a substitute for operator-attested verification.

Run verify_agent.sh first. This script exists only for CI smoke.
EOM
    [[ "$UNATTENDED" == "0" ]] && exit 0
fi

# --unattended-no-witness path: emit a structural smoke check with every
# line stamped UNWITNESSED so it cannot be mistaken for attested truth.
stamp() {
    printf "[UNWITNESSED %s] %s\n" "$(date '+%H:%M:%S')" "$*"
}

stamp "================================================================"
stamp "  sensei_verify.sh — UNATTENDED STRUCTURAL SMOKE"
stamp "  This is NOT a verification. Per operator policy, an unattended"
stamp "  result is hallucinated regardless of any ✓ shown below."
stamp "  Run: bash ~/scripts/verify_agent.sh for attested verification."
stamp "================================================================"

# Boot probes
curl -fsS --max-time 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1 \
    && stamp "ollama :11434  reachable" \
    || stamp "ollama :11434  DOWN"

curl -fsS --max-time 3 http://127.0.0.1:8000/ >/dev/null 2>&1 \
    && stamp "claf :8000     reachable" \
    || stamp "claf :8000     DOWN"

curl -fsS --max-time 3 http://127.0.0.1:8080/health >/dev/null 2>&1 \
    && stamp "bridge :8080   reachable" \
    || stamp "bridge :8080   DOWN"

# Mode read
MODE=$(curl -fsS --max-time 3 http://127.0.0.1:8000/healthz 2>/dev/null \
    | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("config",{}).get("mode","?"))
except: print("?")' 2>/dev/null)
stamp "CLAF reports SENSEI mode = $MODE"

# Queue round-trip
PUSH=$(curl -fsS --max-time 3 -X POST http://127.0.0.1:8080/extension/queue \
    -H 'Content-Type: application/json' \
    -d '{"session_id":"sensei-verify-smoke","actions":[{"kind":"BROWSER_READ"}]}' 2>/dev/null)
AID=$(echo "$PUSH" | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["action_ids"][0])
except: pass' 2>/dev/null)
if [[ -n "$AID" ]]; then
    stamp "queue push       OK   action_id=$AID"
else
    stamp "queue push       FAIL"
fi
POP=$(curl -fsS --max-time 3 "http://127.0.0.1:8080/extension/queue?session_id=sensei-verify-smoke" 2>/dev/null \
    | python3 -c 'import sys,json
try: print(len(json.load(sys.stdin).get("actions",[])))
except: print(0)' 2>/dev/null)
stamp "queue pop        returned $POP action(s)"

# Verdict — explicitly NOT a PASS claim.
stamp "----------------------------------------------------------------"
stamp "STRUCTURAL SMOKE COMPLETE — verdict = UNWITNESSED"
stamp "This output is not a verification. Run verify_agent.sh."
exit 0
