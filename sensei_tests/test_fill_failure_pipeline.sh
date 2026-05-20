#!/usr/bin/env bash
# Synthetic Layer-4 test for BROWSER_FILL false-success regression.
#
# Posts a known-failure payload directly through the bridge action_result
# endpoint and asserts the resulting audit entry preserves the failure
# verdict and the value_did_not_persist reason. Independent of Chrome —
# catches Layer 2/3 regressions without needing the operator at the keyboard.
#
# Pass: prints "LAYER 4 PASS" and exits 0.
# Fail: prints "LAYER 4 FAIL" + raw audit line and exits 1.
#
# Usage: bash ~/scripts/sensei_tests/test_fill_failure_pipeline.sh

set -u
AUDIT_LOG="$HOME/.sensei_bridge_audit.jsonl"
BRIDGE="http://127.0.0.1:8080"
TS=$(date +%s%N)
AID="layer4-test-${TS}"

BODY=$(cat <<EOF
{
  "action_id": "${AID}",
  "session_id": "layer4-test",
  "action": {
    "kind": "BROWSER_FILL",
    "target": "[name=\"email\"]",
    "value": "317-332-4554",
    "id": "${AID}"
  },
  "verdict": "accept",
  "result": "failure",
  "final_state": {
    "ok": false,
    "result": "failure",
    "reason": "value_did_not_persist",
    "expected": "317-332-4554",
    "observed": ""
  }
}
EOF
)

echo "=== Layer 4 — Synthetic failure-pipeline test ==="
echo "action_id: $AID"
echo ""
POST=$(curl -s -X POST "${BRIDGE}/extension/action_result" \
  -H 'Content-Type: application/json' -d "$BODY")
echo "POST response: $POST"
echo ""
sleep 1

LINE=$(grep "${AID}" "$AUDIT_LOG" 2>/dev/null | tail -1)
if [[ -z "$LINE" ]]; then
    echo "LAYER 4 FAIL: no audit entry written for action_id=$AID"
    exit 1
fi

RESULT=$(echo "$LINE" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('result',''))" 2>/dev/null)
REASON=$(echo "$LINE" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print((d.get('final_state') or {}).get('reason',''))" 2>/dev/null)
SID=$(echo "$LINE" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('session_id',''))" 2>/dev/null)
EXPECTED=$(echo "$LINE" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print((d.get('final_state') or {}).get('expected',''))" 2>/dev/null)
OBSERVED=$(echo "$LINE" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print((d.get('final_state') or {}).get('observed',''))" 2>/dev/null)

echo "Audit entry parsed:"
echo "  result:     $RESULT"
echo "  reason:     $REASON"
echo "  session_id: $SID"
echo "  expected:   $EXPECTED"
echo "  observed:   ${OBSERVED:-(empty)}"
echo ""

if [[ "$RESULT" == "failure" \
   && "$REASON" == "value_did_not_persist" \
   && "$SID" == "layer4-test" \
   && "$EXPECTED" == "317-332-4554" \
   && -z "$OBSERVED" ]]; then
    echo "LAYER 4 PASS: failure payload preserved through pipeline"
    exit 0
fi

echo "LAYER 4 FAIL: pipeline mutated the failure payload"
echo "Raw audit line:"
echo "$LINE"
exit 1
