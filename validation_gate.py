"""Pre-dispatch validation gate (Tier-1 typed dispatch boundary).

Extracted 2026-10-05 from master_ai.py along the gate seam, same discipline
as the sensei_tables.py / runtime_state.py extractions: this module never
imports master_ai (master_ai imports IT), so `log` is replicated locally
(byte-identical format) in the action_validation.py standalone convention.

DISPATCH-MIGRATION STATE — LIVE vs SHADOW (evidence pinned 2026-10-05;
read this before "finishing" the migration by deleting either half):

LIVE: gate(collected) runs on every collected directive list inside
process_reply (master_ai.py), BEFORE the first dispatch consumption. Its
verdict gates dispatch: blocked payloads are removed from the lists, logged
as TOOL_BLOCKED_VALIDATION, printed, fed back into history as [TOOL BLOCKED]
(including the REMEMBER: invitation), and recorded in _LAST_BLOCKED_ACTION.
The validator underneath is action_validation.validate_action() — shape
checks for RUN/RUNTERM/READ/CREATE/EDIT/MCP_CALL with syntax verification
of CREATE/EDIT content. Reachability proof: master_ai.py calls
_validation_gate (thin wrapper over gate()) at the pre-dispatch site, and
tests/test_master_ai_core.py::TestValidationGate pins the contract
(valid commands survive, malformed blocked, validator crash fails closed,
missing module fails closed).

SHADOW: typed_actions.parse_reply() is still only shadow-parsed inside
process_reply (results land in _LAST_TYPED_ACTIONS/_LAST_TYPED_ACTIONS_ERROR
for observability/tests). It is NOT on the dispatch path.

Why the legacy regex extractor cannot be retired yet — verified, not
assumed; each item is a real counterexample to "delete the legacy branch":

1. test_typed_actions_parity.py proves typed_actions.parse_reply_with_bodies
   agrees with hand-verified fixtures, NOT with the live extractor — its own
   docstring states it never imports master_ai and "will NOT catch
   [extraction drift] automatically ... re-verify the expected values by
   hand against master_ai.py's current extraction before trusting a green
   run as proof of parity". A green parity run is not proof of redundancy.
2. The two parsers diverge on at least one live shape: a bare "RUN:" with
   no payload — typed parse rejects it and the legacy extractor drops it at
   a different point in the chain (test_typed_dispatch_e2e.py skips on
   exactly this shape: "parser drops a bare RUN: before validation sees
   it"). Shapes must be pinned by parity fixtures that import master_ai's
   real extractor before any selector swap.
3. The legacy extractor selects kinds the typed parser has no envelope for
   (RUN_SKILL, SUBAGENT, MCP_CALL, REMEMBER, TASK_ADD/DONE, SEND_EMAIL/
   SEND_TELEGRAM, BROWSER_*, malformed-EDIT repair round-trips). The gate
   deliberately passes unmodelled payloads through untouched (load-bearing:
   test_unmodelled_list_passes_untouched) — a typed-selector swap today
   would silently drop directives the legacy path handles.

Retirement path once the above are closed: swap the selector (extraction)
to typed parse kind-by-kind behind parity fixtures that import master_ai's
real extractor, keep this gate as the pre-dispatch validator, then delete
the legacy regex buckets. Each flip needs its own green parity+e2e run.
"""

from __future__ import annotations

from datetime import datetime

from sensei_tables import _GATE_KIND_BY_LIST, LOG_FILE

__all__ = ["gate"]


def _log(msg: str) -> None:
    # Mirrors master_ai.log() / sensei_tables._fmt_ampm(seconds=True) exactly;
    # kept local so this module stays master_ai-free (action_validation
    # convention — extracted modules must not import master_ai).
    ts = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def gate(collected: dict) -> tuple:
    """Run every collected directive through the pre-dispatch validator.

    CLAUDE.md's Tier-1 blocker: nothing validated the shape of an action
    between "the model emitted a directive" and "we execute it", so a
    malformed or unparseable action was dispatched anyway and the model went
    on to report the step as done.

    `collected` is a mapping of list-name -> list of payloads as assembled by
    process_reply's extraction (plain strings, or (path, content) tuples for
    CREATE/EDIT). Returns (kept, blocked) where `kept` is a new mapping with
    the invalid entries removed, preserving order, and `blocked` is a list of
    (list_name, payload, reason) for feedback into history.

    Legacy extraction still selects WHAT to run — it is the battle-tested
    path, and retiring it is NOT yet proven safe: see the module docstring
    for the pinned evidence (parity fixtures that never import master_ai, a
    live bare-RUN: shape divergence, and unmodelled kinds the gate must pass
    through). The typed parse is LIVE in the sense that this gate's verdict
    gates dispatch; wholesale selector replacement remains future work gated
    on real parity coverage.

    Fail-closed on every boundary it controls: action_validation
    unimportable -> block ALL payloads; validate_action raising -> block
    that payload. An exception here must never propagate and crash the turn.
    """
    # Lazy import is load-bearing, not style: the fail-closed contract is
    # tested by patching sys.modules AT CALL TIME
    # (test_missing_validation_module_fails_closed), which only a call-time
    # import can observe. A module-scope import would bind the real module
    # long before any patch and silently break the fail-closed guarantee —
    # it would also make this module unimportable whenever action_validation
    # is missing, instead of degrading to block-everything.
    try:
        import action_validation as _av
    except Exception as e:  # noqa: BLE001 - fail-closed: a missing module blocks ALL dispatch
        _log(f"⚠ FAIL-CLOSED VALIDATION_GATE_UNAVAILABLE: {e}")
        all_blocked = [
            (name, payload, "validation gate unavailable — fail-closed")
            for name, items in collected.items()
            for payload in items or []
        ]
        return {}, all_blocked

    kept, blocked = {}, []
    for name, items in collected.items():
        survivors = []
        for payload in items or []:
            if (
                name == "create_files"
                and isinstance(payload, (tuple, list))
                and payload
            ):
                kind, target, content = "CREATE", payload[0], payload[1]
            elif name == "edit_ops" and isinstance(payload, (tuple, list)) and payload:
                kind, target, content = (
                    "EDIT",
                    payload[0],
                    (payload[2] if len(payload) > 2 else None),
                )
            else:
                # Explicit map, NOT derived from the list name: these names
                # are plural and snake-cased ("run_cmds"), so deriving a kind
                # from them produced RUN_CMDS, matched nothing in the
                # validator, and silently let every payload through.
                kind = _GATE_KIND_BY_LIST.get(name, "")
                target, content = payload, None
            if not kind:
                survivors.append(payload)
                continue
            try:
                res = _av.validate_action(
                    {"kind": kind, "target": target, "create_content": content}
                )
            except Exception as e:  # noqa: BLE001 - fail-closed
                _log("FAIL-CLOSED VALIDATOR_CRASH: " + str(e))
                blocked.append(
                    (name, payload, "validator crashed - fail-closed: " + str(e))
                )
                continue
            if res.ok:
                survivors.append(payload)
            else:
                blocked.append((name, payload, res.reason))
        kept[name] = survivors
    return kept, blocked
