#!/usr/bin/env bash
# verify_agent.sh — operator-gated, foreground-anchored, witness-attested
#   verification of MADAM + SENSEI.
#
# Per the No Phantom Results Policy: every step locks until the operator
# presses ENTER, the action executes, then locks again for y/N attestation.
# UNWITNESSED steps are treated as hallucinated regardless of any log.
#
# Six requirements honored:
#   1. Display Surface Anchoring — TTY required; foreground-only.
#   2. Step-Gated Execution — per-step ENTER + attest gate.
#   3. Visual State Representation — banner with step #, name, expected
#      behavior, PIDs/ports/provider/mode, live timestamp.
#   4. Human Attestation Gate — y/N after every step; N halts.
#   5. Screen Evidence Anchoring — screenshot per step to
#      ~/sensei_verify_evidence/ (scrot preferred, gnome-screenshot
#      fallback). Step ID + timestamp in filename.
#   6. Verdict derived from witnessed state, NOT log parsing.
#
# Usage:
#   bash ~/scripts/verify_agent.sh             # full live, gated run
#   bash ~/scripts/verify_agent.sh --dry-run   # banners + gates, no live calls
#   bash ~/scripts/verify_agent.sh --no-screenshot
#   bash ~/scripts/verify_agent.sh --show-spec
#
# Exit:
#   0 = all steps WITNESSED (or dry-run complete)
#   1 = setup error (no TTY, missing dependency)
#   2 = at least one step UNWITNESSED — verdict FAIL
#   3 = operator aborted (Ctrl-C or N on attestation)

set +e
set -u

# ----- args -----
DRY_RUN=0
NO_SHOT=0
SHOW_SPEC=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)        DRY_RUN=1 ;;
        --no-screenshot)  NO_SHOT=1 ;;
        --show-spec)      SHOW_SPEC=1 ;;
        -h|--help)
            sed -n '2,25p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 1
            ;;
    esac
done

SPEC="$HOME/scripts/sensei_tests/verify_agent_autoloop.txt"
CONSUMER_SPEC="$HOME/scripts/sensei_tests/agent_autoloop_consumer_test.txt"
EVID_DIR="$HOME/sensei_verify_evidence"
WITNESS_LOG="$EVID_DIR/witness_log.jsonl"

if [[ "$SHOW_SPEC" == "1" ]]; then
    [[ -f "$SPEC" ]] && cat "$SPEC" || echo "spec missing: $SPEC"
    echo ""
    echo "---"
    [[ -f "$CONSUMER_SPEC" ]] && cat "$CONSUMER_SPEC" || echo "consumer spec missing: $CONSUMER_SPEC"
    exit 0
fi

# ----- requirement 1: Display Surface Anchoring (TTY required) -----
# In live mode the script MUST run in a foreground terminal so the operator
# can witness each step. Dry-run can be piped because no live execution.
if [[ "$DRY_RUN" == "0" ]]; then
    if [[ ! -t 0 ]] || [[ ! -t 1 ]]; then
        cat <<'EOM' >&2
ERROR — Display Surface Anchoring violated.

verify_agent.sh must run in a foreground terminal so the operator can
visually witness each step. It is not safe to background, pipe, or
detach this script. The No Phantom Results Policy requires that
unwitnessed steps be classified as hallucinated.

Open a terminal you can see, then run:
    bash ~/scripts/verify_agent.sh

To preview banners and gating without touching live services:
    bash ~/scripts/verify_agent.sh --dry-run
EOM
        exit 1
    fi
fi

mkdir -p "$EVID_DIR"

# Detect screenshot tool (scrot fast, gnome-screenshot fallback).
SHOT_TOOL=""
if [[ "$NO_SHOT" == "0" ]]; then
    for t in scrot gnome-screenshot import grim maim; do
        if command -v "$t" >/dev/null 2>&1; then
            SHOT_TOOL="$t"
            break
        fi
    done
fi

# ----- state tracking -----
declare -a STEP_NAMES
declare -a STEP_STATUS   # WITNESSED | UNWITNESSED | DRY-RUN
declare -a STEP_EVIDENCE
TOTAL_STEPS=10
CURRENT_STEP=0

write_witness() {
    # Append a JSONL line to the witness log.
    local step="$1" name="$2" status="$3" evidence="${4:-}"
    printf '{"ts":"%s","step":%s,"name":"%s","status":"%s","evidence":"%s","dry_run":%s}\n' \
        "$(date -Iseconds)" "$step" "${name//\"/\\\"}" "$status" "${evidence//\"/\\\"}" "$DRY_RUN" \
        >> "$WITNESS_LOG"
}

# ----- requirement 3: Visual State Representation -----
banner() {
    local n="$1" name="$2" expected="$3"
    clear
    echo "================================================================"
    echo "                    SENSEI / MADAM VERIFICATION"
    echo "================================================================"
    echo ""
    echo "  STEP $n of $TOTAL_STEPS"
    echo "  --------"
    echo "  Name:        $name"
    echo "  Expected:    $expected"
    echo "  Timestamp:   $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo ""
    echo "  Current system state"
    echo "  --------------------"
    local mode bridge_up claf_up bridge_pid claf_pid ollama_up
    mode=$(curl -fsS --max-time 2 http://127.0.0.1:8000/healthz 2>/dev/null \
        | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(d.get("config",{}).get("mode","?"))
except Exception:
    print("?")' 2>/dev/null)
    claf_up=$(curl -fsS --max-time 2 http://127.0.0.1:8000/ >/dev/null 2>&1 && echo up || echo DOWN)
    bridge_up=$(curl -fsS --max-time 2 http://127.0.0.1:8080/health >/dev/null 2>&1 && echo up || echo DOWN)
    ollama_up=$(curl -fsS --max-time 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1 && echo up || echo DOWN)
    claf_pid=$(pgrep -f orchestrator.py | head -1)
    bridge_pid=$(pgrep -f sensei_bridge.py | head -1)
    printf "  Ollama (:11434):     %s\n" "$ollama_up"
    printf "  CLAF (:8000):        %s   pid=%s   SENSEI mode=%s\n" "$claf_up" "${claf_pid:-none}" "${mode:-?}"
    printf "  Sensei bridge :8080: %s   pid=%s\n" "$bridge_up" "${bridge_pid:-none}"
    echo "  Evidence dir:        $EVID_DIR"
    [[ -n "$SHOT_TOOL" ]] && echo "  Screenshot tool:     $SHOT_TOOL" || echo "  Screenshot tool:     none (no evidence captured)"
    [[ "$DRY_RUN" == "1" ]] && echo "  Mode:                DRY-RUN (no live services touched)"
    echo ""
    echo "================================================================"
    echo ""
}

# ----- requirement 2: Step-Gated Execution -----
wait_enter() {
    local n="$1"
    if [[ "$DRY_RUN" == "1" ]]; then
        read -r -p "[DRY-RUN] STEP $n — Press ENTER to preview..."
    else
        read -r -p "STEP $n — Press ENTER to execute (or Ctrl-C to abort)..."
    fi
}

# ----- requirement 5: Screen Evidence Anchoring -----
capture_evidence() {
    local n="$1"
    if [[ "$DRY_RUN" == "1" ]] || [[ "$NO_SHOT" == "1" ]] || [[ -z "$SHOT_TOOL" ]]; then
        STEP_EVIDENCE[$n]=""
        return 0
    fi
    local ts fname
    ts=$(date +%Y%m%d_%H%M%S)
    fname="$EVID_DIR/step_${n}_${ts}.png"
    case "$SHOT_TOOL" in
        scrot)            scrot --quality 60 "$fname" 2>/dev/null ;;
        gnome-screenshot) gnome-screenshot -f "$fname" 2>/dev/null ;;
        import)           import -window root "$fname" 2>/dev/null ;;
        grim)             grim "$fname" 2>/dev/null ;;
        maim)             maim "$fname" 2>/dev/null ;;
    esac
    if [[ -f "$fname" ]]; then
        STEP_EVIDENCE[$n]="$fname"
        echo ""
        echo "  📸 evidence captured: $fname"
    else
        STEP_EVIDENCE[$n]=""
        echo ""
        echo "  ⚠  evidence capture failed (no screenshot saved)"
    fi
}

# ----- requirement 4: Human Attestation Gate -----
attest() {
    local n="$1"
    if [[ "$DRY_RUN" == "1" ]]; then
        echo ""
        echo "[DRY-RUN] would prompt: 'STEP $n COMPLETE — Did you visually observe this result on screen? [y/N]'"
        STEP_STATUS[$n]="DRY-RUN"
        write_witness "$n" "${STEP_NAMES[$n]}" "DRY-RUN" "${STEP_EVIDENCE[$n]:-}"
        read -r -p "[DRY-RUN] Press ENTER to advance to next step..."
        return 0
    fi
    local resp
    echo ""
    read -r -p "STEP $n COMPLETE — Did you visually observe this result on screen? [y/N] " resp
    case "$resp" in
        y|Y|yes|YES|Yes)
            STEP_STATUS[$n]="WITNESSED"
            write_witness "$n" "${STEP_NAMES[$n]}" "WITNESSED" "${STEP_EVIDENCE[$n]:-}"
            echo "  ✓ witnessed"
            ;;
        *)
            STEP_STATUS[$n]="UNWITNESSED"
            write_witness "$n" "${STEP_NAMES[$n]}" "UNWITNESSED" "${STEP_EVIDENCE[$n]:-}"
            echo ""
            echo "============================================================"
            echo "  STEP $n FLAGGED UNWITNESSED — halting per No Phantom"
            echo "  Results Policy. Verdict: FAIL."
            echo "============================================================"
            summary
            exit 2
            ;;
    esac
}

# Combined step driver: banner → ENTER gate → run body → screenshot → attest
do_step() {
    local n="$1" name="$2" expected="$3" body_fn="$4"
    CURRENT_STEP="$n"
    STEP_NAMES[$n]="$name"
    banner "$n" "$name" "$expected"
    wait_enter "$n"
    if [[ "$DRY_RUN" == "1" ]]; then
        echo ""
        echo "[DRY-RUN] would now execute step $n body ($body_fn)"
        echo "[DRY-RUN] (skipped — no live services touched)"
    else
        echo ""
        echo "--- step $n executing ---"
        $body_fn
        echo "--- step $n action complete ---"
    fi
    capture_evidence "$n"
    attest "$n"
}

# ============================================================
# STEP BODIES — each is the real action the operator witnesses
# ============================================================

step1_boot() {
    echo "Curling each required service on its real port:"
    echo ""
    echo "  ollama  http://127.0.0.1:11434/api/version"
    curl -fsS --max-time 3 http://127.0.0.1:11434/api/version || echo "  ✗ ollama unreachable"
    echo ""
    echo "  claf    http://127.0.0.1:8000/"
    curl -fsS --max-time 3 http://127.0.0.1:8000/ || echo "  ✗ claf unreachable"
    echo ""
    echo "  bridge  http://127.0.0.1:8080/health"
    curl -fsS --max-time 3 http://127.0.0.1:8080/health || echo "  ✗ bridge unreachable"
}

step2_mode_visible() {
    echo "Reading SENSEI mode from CLAF /healthz:"
    echo ""
    curl -fsS --max-time 3 http://127.0.0.1:8000/healthz \
        | python3 -m json.tool 2>/dev/null | head -30
}

# Helper for mode-switch steps: write the systemd EnvironmentFile, restart.
switch_claf_mode() {
    local mode="$1"
    mkdir -p "$HOME/.config/systemd/user"
    echo "CLAF_MODE=$mode" > "$HOME/.config/systemd/user/claf.env"
    systemctl --user restart claf.service >/dev/null 2>&1
    sleep 2
}

step3_local_route() {
    echo "Switching CLAF to LOCAL mode..."
    switch_claf_mode local
    echo "Active mode:"
    curl -fsS http://127.0.0.1:8000/healthz \
        | python3 -c 'import sys,json; print("  mode =", json.load(sys.stdin)["config"]["mode"])'
    echo ""
    echo "Sending a routine request to /v1/messages..."
    curl -fsS -X POST http://127.0.0.1:8000/v1/messages \
        -H 'Content-Type: application/json' -H 'x-api-key: dummy' -H 'anthropic-version: 2023-06-01' \
        -d '{"model":"claude-sonnet-4-5","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}' \
        >/dev/null
    echo ""
    echo "Most recent route_decision line in orchestrator.log:"
    grep '"event": "route_decision"' "$HOME/projects/claf/orchestrator.log" 2>/dev/null \
        | tail -1 | python3 -m json.tool
}

step4_local_refuses_cloud() {
    echo "(still in LOCAL mode)"
    echo "Sending a request with metadata.escalate=true — must be refused with 423:"
    echo ""
    curl -s -w "\nHTTP_STATUS=%{http_code}\n" -X POST http://127.0.0.1:8000/v1/messages \
        -H 'Content-Type: application/json' -H 'x-api-key: dummy' -H 'anthropic-version: 2023-06-01' \
        -d '{"model":"claude-sonnet-4-5","max_tokens":5,"metadata":{"escalate":true},"messages":[{"role":"user","content":"hi"}]}'
    echo ""
    echo "Expected: HTTP_STATUS=423 with type=mode_lock."
}

step5_hybrid_modes() {
    echo "Switching CLAF to HYBRID mode..."
    switch_claf_mode hybrid
    echo ""
    echo "(a) routine prompt — expect provider=local-ollama"
    curl -fsS -X POST http://127.0.0.1:8000/v1/messages \
        -H 'Content-Type: application/json' -H 'x-api-key: dummy' -H 'anthropic-version: 2023-06-01' \
        -d '{"model":"claude-sonnet-4-5","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}' \
        >/dev/null
    grep '"event": "route_decision"' "$HOME/projects/claf/orchestrator.log" 2>/dev/null \
        | tail -1 | python3 -m json.tool
    echo ""
    echo "(b) explicit escalate — expect provider=<cloud peer>, cloud_escalated=true"
    curl -fsS -X POST http://127.0.0.1:8000/v1/messages \
        -H 'Content-Type: application/json' -H 'x-api-key: dummy' -H 'anthropic-version: 2023-06-01' \
        -d '{"model":"claude-sonnet-4-5","max_tokens":5,"metadata":{"escalate":true},"messages":[{"role":"user","content":"hi"}]}' \
        >/dev/null
    grep '"event": "route_decision"' "$HOME/projects/claf/orchestrator.log" 2>/dev/null \
        | tail -1 | python3 -m json.tool
}

step6_cloud_bypass() {
    echo "Switching CLAF to CLOUD mode..."
    switch_claf_mode cloud
    echo "Sending request — expect provider=<cloud peer>, local_attempted=false:"
    curl -fsS -X POST http://127.0.0.1:8000/v1/messages \
        -H 'Content-Type: application/json' -H 'x-api-key: dummy' -H 'anthropic-version: 2023-06-01' \
        -d '{"model":"claude-sonnet-4-5","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}' \
        >/dev/null
    grep '"event": "route_decision"' "$HOME/projects/claf/orchestrator.log" 2>/dev/null \
        | tail -1 | python3 -m json.tool
    echo ""
    echo "Restoring mode to HYBRID for the rest of the suite..."
    switch_claf_mode hybrid
}

step7_mcp_headless() {
    echo "Driving the MCP server via stdio (no Claude Code, no Codex, no Anthropic):"
    echo ""
    local resp
    resp=$(printf '%s\n%s\n%s\n' \
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"browser.navigate","arguments":{"url":"about:blank","wait_seconds":1}}}' \
        | timeout 8 python3 "$HOME/scripts/sensei_mcp_server.py" 2>/dev/null)
    echo "$resp" | grep -Eo '"name":"[^"]+"' | sort -u | head -10
    echo ""
    echo "Last tools/call response (truncated):"
    echo "$resp" | tail -1 | head -c 400
    echo ""
}

step8_queue_round_trip() {
    echo "Bridge /extension/queue push then pop:"
    echo ""
    local push aid
    push=$(curl -fsS -X POST http://127.0.0.1:8080/extension/queue \
        -H 'Content-Type: application/json' \
        -d '{"session_id":"verify-step8","actions":[{"kind":"BROWSER_NAV","target":"https://example.com"}]}')
    echo "push response:"
    echo "$push" | python3 -m json.tool
    aid=$(echo "$push" | python3 -c 'import sys,json; print(json.load(sys.stdin)["action_ids"][0])')
    echo ""
    echo "pop (GET /extension/queue?session_id=verify-step8):"
    curl -fsS "http://127.0.0.1:8080/extension/queue?session_id=verify-step8" | python3 -m json.tool
    echo ""
    echo "second GET — should now be empty:"
    curl -fsS "http://127.0.0.1:8080/extension/queue?session_id=verify-step8" | python3 -m json.tool
}

step9_result_loop() {
    echo "Push → simulate Chrome posting action_result → GET /extension/result:"
    echo ""
    local push aid
    push=$(curl -fsS -X POST http://127.0.0.1:8080/extension/queue \
        -H 'Content-Type: application/json' \
        -d '{"session_id":"verify-step9","actions":[{"kind":"BROWSER_NAV","target":"https://example.com"}]}')
    aid=$(echo "$push" | python3 -c 'import sys,json; print(json.load(sys.stdin)["action_ids"][0])')
    echo "action_id: $aid"
    echo ""
    echo "GET before action_result (expect status=pending, HTTP 404):"
    curl -s -w "\nHTTP=%{http_code}\n" "http://127.0.0.1:8080/extension/result?action_id=$aid"
    echo ""
    echo "Simulating Chrome posting action_result..."
    curl -fsS -X POST http://127.0.0.1:8080/extension/action_result \
        -H 'Content-Type: application/json' \
        -d "{\"action_id\":\"$aid\",\"verdict\":\"accept\",\"result\":\"success\",\"final_state\":{\"url\":\"https://example.com\"}}"
    echo ""
    echo "GET after action_result:"
    curl -fsS "http://127.0.0.1:8080/extension/result?action_id=$aid" | python3 -m json.tool
}

step10_live_chrome() {
    cat <<'EOM'
This step requires your Sensei extension side panel to be open and polling.
The action will land in your panel within ~3 seconds. Watch your tab.

EOM
    local push aid r http
    push=$(curl -fsS -X POST http://127.0.0.1:8080/extension/queue \
        -H 'Content-Type: application/json' \
        -d '{"session_id":"mcp-default","actions":[{"kind":"BROWSER_READ"}]}')
    aid=$(echo "$push" | python3 -c 'import sys,json; print(json.load(sys.stdin)["action_ids"][0])')
    echo "Queued BROWSER_READ → action_id=$aid"
    echo ""
    echo "Waiting up to 15s for your panel to dispatch and post back the result..."
    for i in $(seq 1 15); do
        http=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8080/extension/result?action_id=$aid")
        if [[ "$http" == "200" ]]; then
            echo ""
            echo "Panel result returned at t+${i}s:"
            curl -fsS "http://127.0.0.1:8080/extension/result?action_id=$aid" \
                | python3 -c 'import sys,json
d=json.load(sys.stdin)
r=d.get("result",{})
print("  verdict:    ", r.get("verdict"))
print("  result:     ", r.get("result"))
fs=r.get("final_state",{}) or {}
print("  url:        ", fs.get("url") or fs.get("observed_tab_url"))
print("  title:      ", fs.get("title",""))
txt=fs.get("text","") or ""
if txt: print("  text head:  ", txt[:120].replace("\n"," "))'
            return 0
        fi
        sleep 1
    done
    echo ""
    echo "Panel did not return a result within 15s. Likely causes:"
    echo "  - Side panel not open (open Chrome → Sensei extension icon → side panel)"
    echo "  - Backend disconnected from panel (check Connection indicator at top)"
    echo "  - Panel in Plan mode (switch to Auto or Review and approve the card)"
}

# ============================================================
# SUMMARY
# ============================================================

summary() {
    echo ""
    echo "================================================================"
    echo "                    VERIFICATION SUMMARY"
    echo "================================================================"
    local wit=0 unwit=0 dry=0 notrun=0
    for n in $(seq 1 $TOTAL_STEPS); do
        local s="${STEP_STATUS[$n]:-NOT_RUN}"
        local nm="${STEP_NAMES[$n]:-(not reached)}"
        local ev="${STEP_EVIDENCE[$n]:-}"
        case "$s" in
            WITNESSED)   ((wit++)) ;;
            UNWITNESSED) ((unwit++)) ;;
            DRY-RUN)     ((dry++)) ;;
            *)           ((notrun++)) ;;
        esac
        if [[ -n "$ev" ]]; then
            printf "  Step %-2s  %-15s  %s\n           %s\n" "$n" "$s" "$nm" "evidence: $ev"
        else
            printf "  Step %-2s  %-15s  %s\n" "$n" "$s" "$nm"
        fi
    done
    echo "  ------------------------------------------------------------"
    echo "  WITNESSED: $wit   UNWITNESSED: $unwit   DRY-RUN: $dry   NOT_RUN: $notrun"
    echo "  Evidence dir: $EVID_DIR"
    echo "  Witness log:  $WITNESS_LOG"
    echo ""
    # Requirement 6: verdict derived from witnessed state, NOT log parsing.
    if [[ "$unwit" -gt 0 ]]; then
        echo "  VERDICT: FAIL"
        echo "  Reason: $unwit step(s) were UNWITNESSED. Per No Phantom Results"
        echo "  Policy, an unwitnessed step is hallucinated regardless of any log."
        return 1
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  VERDICT: DRY-RUN COMPLETE"
        echo "  Reason: gating + banners previewed; no live verification performed."
        echo "  Run without --dry-run to record witnessed evidence."
        return 0
    fi
    if [[ "$wit" -lt "$TOTAL_STEPS" ]]; then
        echo "  VERDICT: PARTIAL"
        echo "  Reason: only $wit of $TOTAL_STEPS steps witnessed."
        return 1
    fi
    echo "  VERDICT: WITNESSED PASS"
    echo "  All $TOTAL_STEPS steps observed by operator with attestation."
    return 0
}

# Graceful Ctrl-C
on_abort() {
    echo ""
    echo ""
    echo "================================================================"
    echo "  OPERATOR ABORT (Ctrl-C) at step $CURRENT_STEP"
    echo "================================================================"
    [[ "$CURRENT_STEP" -gt 0 ]] && {
        STEP_STATUS[$CURRENT_STEP]="UNWITNESSED"
        write_witness "$CURRENT_STEP" "${STEP_NAMES[$CURRENT_STEP]:-?}" "ABORTED" ""
    }
    summary
    exit 3
}
trap on_abort INT TERM

# ============================================================
# RUN
# ============================================================

clear
cat <<'EOM'
================================================================
            SENSEI / MADAM — OPERATOR-GATED VERIFICATION
================================================================

You are the primary reviewer. Each step pauses for ENTER, executes,
then asks you to attest with y/N that you saw the result on screen.

Six guarantees:
  1. Foreground terminal only (no background, no detached).
  2. Per-step ENTER gate.
  3. Banner shows step #, expected behavior, live system state.
  4. y/N attestation after every step. N halts immediately.
  5. Screenshot captured to ~/sensei_verify_evidence/ per step.
  6. Verdict derived from witnessed state, NOT log parsing.

EOM
if [[ "$DRY_RUN" == "1" ]]; then
    echo "  Running in --dry-run: banners + gates only. No live services."
    echo ""
fi
read -r -p "Press ENTER to begin verification (or Ctrl-C to abort)..."

do_step 1  "Required services up"                  "ollama + CLAF + sensei_bridge all return 200"      step1_boot
do_step 2  "SENSEI mode visible"                    "active mode shown in /healthz"                     step2_mode_visible
do_step 3  "LOCAL routing"                          "route_reason=local_mode_only, provider=local-ollama" step3_local_route
do_step 4  "LOCAL refuses cloud (423 mode_lock)"    "HTTP_STATUS=423 with type=mode_lock"               step4_local_refuses_cloud
do_step 5  "HYBRID routine + escalate"              "routine→local-ollama; escalate→cloud peer"         step5_hybrid_modes
do_step 6  "CLOUD bypasses local"                   "cloud_mode_bypass_local, local_attempted=false"    step6_cloud_bypass
do_step 7  "MCP headless (no Anthropic/Codex)"      "tools/list + tools/call return real results"       step7_mcp_headless
do_step 8  "Bridge queue push/pop"                  "push enqueues, GET pops, second GET empty"         step8_queue_round_trip
do_step 9  "Result loop closes (pending→200)"       "GET 404→post action_result→GET 200"                step9_result_loop
do_step 10 "LIVE: action in your Chrome panel"      "BROWSER_READ lands in panel, result posted back"   step10_live_chrome

summary
exit $?
