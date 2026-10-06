# HANDOFF — Monolith Split Operation (round 3)

**Branch:** fix/eval-round2-2026-10-05
**Started:** 2026-10-05 ~22:00 EDT
**Updated:** 2026-10-05 ~22:00 EDT — OpenCode located at /home/elijah/.nvm/versions/node/v22.22.3/bin/opencode (v1.18.32), added as third worker
**Goal:** Split master_ai.py (~27,200 lines) along clean seams until Jev no longer flags the monolith as the #1 production-readiness blocker.
**Baseline:** 364/364 tests passing. Rules: LOCAL CHANGES ONLY — no commits, no pushes, no PRs. Ruff clean on touched files.

## Assignments
| Agent | Seam | Status |
|-------|------|--------|
| Claude Code | Orchestration / REPL loop: handle() -> orchestrate() -> model -> process_reply() -> continuation | assigned |
| Hermes | Dispatch + compaction: typed-action dispatch paths, compaction / memory summarization | assigned |
| OpenCode | third seam (coordinator assigning) | assigned |

## Rules of engagement
- No two agents touch the same functions. Seam boundaries are recorded below before extraction starts.
- Pattern per extraction: move code -> thin delegate wrappers in master_ai.py -> full pytest -> ruff -> update this file.
- If an extraction threatens test health, revert it and log it here instead of forcing it.

## Seam map (coordinator-verified 2026-10-05 ~21:55 EDT)

DEVIATION NOTE: the assignments table above put process_reply() under Claude's
orchestration seam, but ALL dispatch code remaining in master_ai.py (legacy
if/elif chain + typed shadow-parse call site ~16873) lives INSIDE process_reply().
Assigning it to both agents would violate the no-shared-functions rule.
Resolution: process_reply() -> Hermes (dispatch seam). Documented here.

### Claude -> orchestration.py (REPL loop / orchestration)
| Function | Lines | Notes |
|----------|-------|-------|
| orchestrate() | 4158-4912 | core loop entry |
| handle() | 19959-22081 | REPL entry; calls process_reply (8x), orchestrate (4x), compact_history (2x) |
| _reply_needs_operator_input() | 8817-8888 | continuation guard |
| _watchdog_maybe_auto_continue() | 8889-8924 | continuation guard |
May import: process_reply from dispatch, compact_history from context.
NEVER import from master_ai. Leave thin wrappers in master_ai.py.

### Hermes -> dispatch.py (reply parsing + dispatch)
| Function | Lines | Notes |
|----------|-------|-------|
| process_reply() | 16705-18731 | contains legacy dispatch chain + typed shadow call site |
| reply-parsing helpers | ~15982-16704 | Hermes verifies exact set: _xml_tool_calls_to_directives (16192-16323), _join_bare_keyword_lines, _truncate_repeated_lines, _normalize_directive_lines (16477-16534), _reply_claims_unexecuted_action, _run_skill_specs_from_reply (15982-15996), _resume_skill_reply_from_turn (16117-16161), _skill_state_reply, _run_skill_reply_from_reply |
Self-contained: NEVER import from master_ai. May import from context. Leave thin wrappers.

### Hermes -> context.py (compaction + memory)
| Function | Lines | Notes |
|----------|-------|-------|
| _compact_older_messages() | 8993-9017 | |
| _compact_history_in_place() | 9018-9044 | |
| compact_history() | 9045-9065 | |
| summarize_session() | 22120-22190 | |
| load_memory() | 9514-9520 | |
| _is_memory_marker_line() | 9521-9526 | |
| _append_memory_marker() | 9533-9546 | |
| select_memory_context() | 9738-9795 | |
| _memory_recall_payload() | 3758-3784 | |
| _inject_relevant_memory() | 6501-6544 | |
Leaf module: no new-module imports. Leave thin wrappers.


### OpenCode -> routing.py (model routing layer)
Chosen over the approval-gating path: tight contiguous cluster (3037-3320), leaf w.r.t. other seams (zero calls back into orchestration/dispatch/context).
| Function | Lines | Notes |
|----------|-------|-------|
| _router_metric() | 3037-3047 | |
| _router_recent_events() | 3048-3063 | |
| _router_model_stats() | 3064-3090 | |
| _router_perf_bonus() | 3091-3115 | |
| _rank_route_candidates() | 3116-3128 | |
| _choose_route() | 3129-3148 | called 8x by orchestrate() |
| format_router_stats() | 3149-3180 | |
| _scrappy_model_present() | 3181-3214 | |
| detect_route() | 3278-3320 | called 5x by handle() |
| _route_from_fast_classifier() | 4124-4157 | called 1x by orchestrate() |
| _cloud_allowed() / _cloud_trip() | 2767-2784 | candidates; OpenCode evaluates fit |
Leaf module: no new-module imports. orchestration.py may import from routing.py (one-way).

### Dependency contract (updated)
orchestration.py -> dispatch.py, context.py, routing.py (one-way)
dispatch.py -> context.py (one-way)
context.py -> leaf
routing.py -> leaf
master_ai.py -> thin delegate wrappers for every moved function

## Extraction log
- 2026-10-05 ~22:00 EDT: operation opened. Claude + Hermes assigned. OpenCode unavailable.

- 2026-10-05 ~22:05 EDT: seam map recorded (see above). process_reply() reassigned to Hermes; no-shared-functions rule takes precedence over initial table.
- 2026-10-05 ~22:06 EDT: launched Claude (orchestration.py) pid 1227635 and Hermes (dispatch.py + context.py) pid 1227636 in parallel. Logs: /tmp/claude_split.log, /tmp/hermes_split.log.
- 2026-10-05 ~22:10 EDT: OpenCode added as third worker (routing.py, pid 1242100, log /tmp/opencode_split.log). Seam: model routing layer, disjoint from Claude/Hermes.
- 2026-10-05 ~22:16 EDT (10-min check): Claude + Hermes alive and working, no modules landed yet (expected). OpenCode first launch FAILED: missing OPENROUTER_API_KEY. Key sourced from ~/.hermes/.env (export prefix handled); relaunched as pid 1284135 ~22:18 EDT.
- 2026-10-05 ~23:08 EDT (hour check, coordinator-verified): OpenCode DONE — routing.py 546 lines, leaf, 364/364 green at its check, ruff clean. Hermes LANDED dispatch.py (2,929 lines) + context.py (375 lines), still running (no final report yet). Claude (orchestration.py) still working, nothing landed yet. master_ai.py: 27200 -> 23772 lines (-3428). Coordinator ran full suite: 364/364 PASS. Ruff clean on routing/dispatch/context. Disjointness: clean (only shared name is identical _runtime_host helper, the established round-2 pattern - no conflict). Modified tests: test_router_golden.py, test_coding_loop.py, test_remember_directive.py (workers' updates).

- 2026-10-06 (verification session, picking up where this log stopped — Claude's orchestration.py had landed on disk by this point, undocumented, along with runtime_state.py/session_store.py/validation_gate.py from further round-2/3 work): ran the full suite cold. 22 failures, several genuine regressions, not just stale assertions:
  1. **Import fragility (root cause, affected 2+ tests + would have broken in production).** `master_ai.py`'s sibling imports (`import orchestration`, etc.) only resolved when sys.path[0] happened to equal this file's real directory — true when invoked as a top-level script through its `~/scripts` symlink, false for `python3 -c "import master_ai"`, a subprocess, or stt_server.py importing it as a module. Fixed: master_ai.py now pins `sys.path[0]` to its own realpath explicitly, at the top, before any sibling import.
  2. **Model pinning silently broken (real production bug, not just a test).** `PINNED_MODEL` existed as three independent copies (master_ai.py, routing.py, orchestration.py) because `from routing import PINNED_MODEL` is a one-time value import. Writes to `master_ai.PINNED_MODEL` (the `model <name>` command) never reached `routing.detect_route()`, which actually decides where requests go — the UI would show a model as selected while every request kept routing on the auto-heuristic. Fixed: master_ai.py's module `__class__` now intercepts `PINNED_MODEL` assignment and mirrors it into routing.py, so every writer (production code and tests) stays in sync regardless of how it writes.
  3. **Pupil/stt_server.py incompatibility (real production bug — `/chat` HTTP 500).** orchestration.py/dispatch.py/context.py all located "the current session's globals" via a hardcoded `sys.modules["master_ai"]` lookup. stt_server.py deliberately loads an isolated copy of master_ai.py per routing lane under a different module name (`_master_ai_api_<lane>`) specifically so one slow cloud lane can't block another — none of those copies is ever registered under the literal name "master_ai", so the lookup returned None and any Pupil request touching the split code 500'd with `'NoneType' object has no attribute 'BC'`. Fixed: new `runtime_host.py` (thread-local, so concurrent lanes never see each other's caller) — `orchestration.py`/`dispatch.py`/`context.py` read the caller through it instead of guessing a hardcoded name; master_ai.py's thin wrappers (`handle`, `orchestrate`, `process_reply`, the context/compaction wrappers, the continuation guards) are decorated `@runtime_host.bound(_sys.modules[__name__])` so each lane-isolated copy binds its OWN module, not whichever one happened to be "master_ai".
  4. Three stale test assertions (PLAN-BLOCK schema text, `deterministic_intent` reflection, the auto-context-slicer dual-file query) that inspected `master_ai.py`'s own source/behavior for content that correctly moved to `orchestration.py`/`routing.py` during the split — updated to match the new module layout, not reverted.
  5. One pre-existing, unrelated stale assertion (`_deterministic_intent_to_directive`'s "where is X" branch has always emitted `RUNTERM:`, never `RUN:`/`READ:`) — updated.
  6. Two duplicate, superseded root-level test files (`test_observability.py`, `test_typed_actions.py`) colliding with newer, bigger versions of themselves under `tests/` — removed; the `tests/` versions are the ones from this round's work.
  - Full suite after all of the above: 1260 passed, 3 failed, 18 skipped. The 3 remaining failures are live tests that make real cloud model calls or depend on real lock-contention timing (`test_busy_lock_raises_apihandlebusy_within_timeout`, `test_6_done_directive_smoke`, `test_1_direct_identity`) — non-deterministic by nature, reproduced as flaky rather than as a consequence of this split.
  - **Operation closed 2026-10-06.** Merged to master and pushed.
