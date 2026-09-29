# Debug Session — 2026-09-27

## Objective
Fix continuation, accuracy, and coherence issues in the master-ai-cli framework.

## Issues Identified (10 total)

### Continuation
1. `_reply_needs_operator_input` raises `NotImplementedError` — auto-nudge completely dead
2. Cloud-only continuation — Ollama replies hitting `num_predict` don't trigger `PENDING_CONTINUATION`
3. `_RELOAD_CARRY_FILE` has no expiry — persists forever if process fails to restart
4. Skill resume is step-level, not state-level

### Accuracy
5. Typed dispatch is shadow-only — `process_reply()` still uses regex, not `typed_actions.parse_reply()`
6. Adversarial refuter returns `True` (placeholder) — convergence verification not wired
7. No fact-checking or hallucination detection — verification is behavioral only

### Coherence
8. Memory append-only, grows forever — no summarization or pruning
9. Session resumption depends entirely on summary quality — poor summary = no context
10. No cross-session learning loop — `learning_loop.py` is read-only

## Phase 1 — COMPLETED

| Fix | File:Line | What changed |
|-----|-----------|--------------|
| Auto-nudge | `master_ai.py:9527` | Implemented `_reply_needs_operator_input` — detects questions, ASK directives, blocked actions, error states |
| Local continuation | `master_ai.py:9203-9270` | Ollama replies hitting `num_predict` auto-continue up to `_MAX_AUTO_CONTINUATIONS` rounds |
| Carry file expiry | `master_ai.py:24595` | `_RELOAD_CARRY_FILE` older than 600s discarded |

## Phase 2 — PENDING (Accuracy)

| # | Issue | Planned Fix |
|---|-------|-------------|
| 5 | Typed dispatch shadow-only | Make `process_reply()` try typed_actions first, fall back to regex |
| 6 | Adversarial refuter placeholder | Wire real refuter: re-prompt model with claim + "Is this true?" |

## Phase 3 — PENDING (Coherence)

| # | Issue | Planned Fix |
|---|-------|-------------|
| 8 | Memory append-only | Add consolidation pass when memory exceeds ~200 lines |
| 9 | Session resumption summary-dependent | Fallback: inject last 10 messages if summary missing |

## Phase 4 — PENDING (Long-term)

| # | Issue | Planned Fix |
|---|-------|-------------|
| 8 | Skill resume step-level | Add checkpointing within steps |
| 9 | No cross-session learning loop | Wire `learning_loop.py` output into `REMEMBER:` injection |

## Key Files
- `master_ai.py` (28k lines) — core agent loop
- `typed_actions.py` — typed action envelope (currently audit-only)
- `workflow_orchestrator.py` — adversarial verification (refuter is placeholder)
- `skill_runtime.py` — skill executor with step-level resume
- `harvest.py` — cloud call caching + few-shot injection
- `learning_loop.py` — read-only failure analysis

## Notes
- Pre-existing syntax error on `master_ai.py:9702` (comment with `2026-09-20:`) — unrelated to changes
- All Phase 1 changes verified with `python3 -m py_compile` (only pre-existing error remains)
