---
name: retry-failure-schema
description: "Standing retry/failure schema v1.3. HARD CAP = 1 attempt per (operation_id, tool). No retries. Fail once → fallback chain immediately: switch_tool → switch_protocol → operator_eyes → operator_hands. 8 error classes. Phase axis. REFLECTION_FAILURE skips to operator_eyes first. PostToolUse hook (PostToolUseFailure does not exist)."
metadata:
  node_type: memory
  type: feedback
  originSessionId: de36b780-0890-4b00-bda6-2ad92f2dec80
---

## The rule (one paragraph)

When a tool call fails, classify it into one of **eight classes** — TRANSIENT, INTERMITTENT, PERMANENT, AUTH, OBSERVABILITY_FAILURE, SEMANTIC_NO_OP, REFLECTION_FAILURE, UNKNOWN — and apply the matching policy. Tag every call with its **phase** (planning / action / reflection / system) — the phase determines whether a retry fixes the problem or just replays a bad plan. **HARD CAP = 1 attempt per (operation_id, tool). No retries. Any failure → immediately run the fallback chain.** Never attempt the same tool twice on the same failure. On stop: `switch_tool → switch_protocol → operator_eyes → operator_hands`. REFLECTION_FAILURE skips straight to `operator_eyes` first (the agent's own self-assessment is the broken part; get ground truth before replanning). All error classes = zero retries.

## Channel C ritual — run at task start

Before the first tool call of any task: read `~/.claude/.retry_state.json` and confirm counters. If any `consecutive_failures > 0`, name them before proceeding. File missing or corrupt = self-heals on next hook fire.

## Why this exists

**2026-05-23 — MEGA OTT click-loop.** A session driving the operator's browser via sensei MCP burned ~30 minutes mashing the "How to earn gift credit" button. All three observability channels degraded simultaneously: screenshot returned `"BROWSER_SCREENSHOT must be handled by background"`, js_eval returned `"failure"` on every input (including `1+1`), and `read` truncated `interactive_elements` at element #2 before any modal content. The agent kept clicking blind. Earlier the same session, a Drive sweep wasted ~10 minutes on rate-limited rclone with no escalation. Operator's verbatim directive: *"set max failed attempts to 4 then rethink"* and *"if you are in MCP using the extension, sensei or secretary, it must be visible"* — refined to a hard cap of 3 attempts.

## How it's enforced — v1.1 defense-in-depth

Four layers, lightest first. **Layer 0 always applies even if Layers 1–3 are broken.**

### Layer 0 — Three channels

**Channel A (this memory):** You're reading it now. Every fresh session auto-loads `MEMORY.md` which links here. Honor the schema even when no other layer is wired.

**Channel B (UserPromptSubmit injector):** `~/.claude/hooks/userpromptsubmit_inject.sh` — prepends `[RETRY_SCHEMA v1.1 OK]` or `[ENFORCEMENT DEAD]` + active failure counters to every user message. Heartbeat freshness check: if `retry_policy_guard.sh` hasn't fired in >120s → `[ENFORCEMENT DEAD]` escalation.

**Channel C (task-start ritual):** Described above.

### Layer 1 — Canonical YAML
`~/.claude/retry_policy.yaml` (human-edited; the source of truth, schema v1.1)
`~/.claude/.retry_policy.json` (auto-gen by `_compile_policy.sh`; do not edit by hand)
`~/.claude/.retry_policy.json.sha256` (checksum written by compiler; hook verifies before reading)

### Layer 2 — Runtime state
`~/.claude/.retry_state.json` — per-(operation_id, tool) failure counters + `last_args_hash` + `last_class` + `last_response_hash` (for SEMANTIC_NO_OP detection) + global circuit_state. Reset by removing the file or by changing the operation_id via `echo NEW_OP > ~/.claude/.current_operation`. flock-protected, atomic writes via `.tmp` → `mv -f`.

### Layer 3 — Active hook
`~/.claude/hooks/retry_policy_guard.sh` registered as **`PostToolUse`** in `~/.claude/settings.json`. (**NOT `PostToolUseFailure` — that event does not exist in Claude Code.**) Hook receives every tool call (success + failure), classifies from `tool_response.is_error`, increments counter, and emits `{"decision":"block","reason":"..."}` + exit 2 when cap is hit. Includes ERR trap (fail-closed on crash → exit 3), flock, atomic writes, state self-healing, YAML→JSON drift detection + checksum, dependency check, pre-flight browser probe, response-hash SEMANTIC_NO_OP, UNKNOWN class, heartbeat.

## Error classes (v1.2 canonical — 8 classes)

| Class | Phase | Retry? | Trigger |
|---|---|---|---|
| TRANSIENT | system | yes, short backoff | HTTP 5xx, ECONNRESET, ETIMEDOUT, EAI_AGAIN |
| INTERMITTENT | system | yes, long backoff + jitter | HTTP 429/509, throttling/rate-limit errors |
| PERMANENT | action | **NO** | HTTP 4xx (400/404/422), validation errors |
| AUTH | system | once-after-refresh, then STOP | HTTP 401/403, TokenExpired |
| OBSERVABILITY_FAILURE | reflection | **NEVER** | Cannot see the result; screenshot bridge broken; js_eval returns "failure"; read truncates before content |
| SEMANTIC_NO_OP | action | stop after 2 | Tool succeeded but state unchanged (identical response hash on consecutive browser calls) |
| REFLECTION_FAILURE (v1.2 new) | reflection | **NEVER** | Agent reports success/state X but external check (operator, screenshot diff, assertion) shows otherwise. Fallback: `operator_eyes → replan → operator_hands` — skip switch_tool entirely |
| UNKNOWN | system | **NEVER** | Unclassified error — non-retryable by default |

**REFLECTION_FAILURE vs SEMANTIC_NO_OP distinction:**
- SEMANTIC_NO_OP = tool ran, but the page/state didn't change (the **tool** did nothing)
- REFLECTION_FAILURE = tool ran, state did change, but the **agent assessed it wrong** (the **agent** is the broken part)

## Per-tool max_attempts overrides (v1.2 — cold-start estimates)

Global default = 3. Browser observability tools get 2 because a third attempt on a dead channel confirms nothing new.

| Tool group | max_attempts | Rationale |
|---|---|---|
| browser_screenshot | **2** | Bridge failures are persistent; 3rd confirms channel is dead, adds no recovery signal |
| browser_eval | **2** | js_eval returning "failure" on trivial expr = OBSERVABILITY_FAILURE; extra attempt just delays STOP |
| browser_read | **2** | Truncation at element #2 is deterministic; 3rd won't render more content |
| browser_click | 3 | Click can succeed on retry if DOM settles after animation |
| browser_fill | 3 | May need one retry for React controlled-component reconciliation |
| browser_navigate | 3 | Redirects + slow page loads justify up to 3 |
| browser_interact | 3 | Generic; keep default |

**Recalibration:** when `~/.sensei_l3_telemetry.jsonl` + `retry_policy.log` have ≥ 50 samples per tool, run `~/.claude/tools/calibrate_max_attempts.py` to compute empirical values from `pass^k` rates.

## Phase axis (v1.2)

Every tool call should be tagged with its phase. The phase determines recovery strategy — retrying a tool fixes an execution failure; retrying with the same plan fixes nothing if the plan was wrong.

| Phase | Meaning | Recovery on failure |
|---|---|---|
| planning | Reading context, searching, forming a plan | If 2+ planning calls fail → replan from scratch |
| action | Executing a step — click, fill, API call, write | Standard retry → fallback_order |
| reflection | Verifying result — screenshot-after-action, assert | REFLECTION_FAILURE possible → operator_eyes first |
| system | Auth, rate-limit, connectivity, bridge health | TRANSIENT/INTERMITTENT/AUTH classes |

Set via `_phase` in operation metadata or `echo "planning" > ~/.claude/.current_phase`. Advisory in v1.2 (logged but not yet hard-blocking). v1.3 will add phase-conditional fallback routing.

## Tool alias normalization (v1.1)

All sensei + claude-in-chrome browser tools share canonical group counters:

| Group key | Tools in group |
|---|---|
| `browser_click` | `mcp__sensei__click`, `mcp__claude-in-chrome__computer` (left_click/right_click/double_click) |
| `browser_fill` | `mcp__sensei__fill`, `mcp__claude-in-chrome__form_input` |
| `browser_screenshot` | `mcp__sensei__screenshot`, `mcp__claude-in-chrome__computer` (screenshot/zoom) |
| `browser_eval` | `mcp__sensei__js_eval`, `mcp__claude-in-chrome__javascript_tool` |
| `browser_read` | `mcp__sensei__read`, `mcp__claude-in-chrome__read_page`, `mcp__claude-in-chrome__find` |
| `browser_navigate` | `mcp__sensei__browse`, `mcp__claude-in-chrome__navigate` |
| `browser_interact` | `mcp__claude-in-chrome__computer` (generic), `mcp__claude-in-chrome__browser_batch` |

When ALL aliases in a group hit `max_attempts` → `fallback_exhaustion` → `operator_hands` immediately.

## Decision flow

```
Tool call → fail → classify
  PERMANENT      → STOP, diagnose request construction
  OBSERVABILITY  → STOP, switch to a visible channel
  UNKNOWN        → STOP, unclassified = non-retryable
  AUTH (≥2x)     → STOP, hand off to operator for re-auth
  attempts ≥ 3   → STOP, run fallback_order
  SEMANTIC_NO_OP → STOP (identical response hash on consecutive browser calls)
  else           → allow Claude to retry
```

## Fallback chain (on any STOP)

1. **switch_tool** — same job, different tool (rclone→sensei, sensei→curl, Canva MCP→sensei web UI)
2. **switch_protocol** — MCP→shell, browser→direct API
3. **operator_eyes** — ask the operator what's on screen
4. **operator_hands** — hand off, operator does it manually

`fallback_exhaustion` (all group aliases at cap) → `operator_hands` immediately.

## Forbidden patterns (the hook catches these)

- Retrying the same tool with the same args more than 3 times within one operation
- Click-looping on unchanged DOM
- Calling `js_eval` after `js_eval` returned `"failure"`
- Calling `js_eval` after `screenshot` failed (paired observability)
- Revisiting the same URL after a 301 redirect loop
- Retrying rclone within 60s of a rate-limit response

## Kill switches

- v1.1: `touch ~/.claude/.retry_kill_switch` — suspends enforcement instantly; UserPromptSubmit injector announces this in every message
- Legacy v1.0: `touch /tmp/retry_policy_disabled` — still honored for compatibility

## How to use the operation_id

Write a string to `~/.claude/.current_operation` to namespace failure counters:
- `echo "biovega-drive-sweep-2026-05-23" > ~/.claude/.current_operation`
- `echo "greenhouse-autofill-run-1" > ~/.claude/.current_operation`

If not set, defaults to `"default"`. Reset counters: `rm ~/.claude/.retry_state.json` OR change operation_id.

## Key files (v1.1)

| File | Purpose |
|---|---|
| `~/.claude/retry_policy.yaml` | Canonical source — human-edited |
| `~/.claude/.retry_policy.json` | Compiled JSON for jq |
| `~/.claude/.retry_policy.json.sha256` | Tamper detection checksum |
| `~/.claude/.retry_state.json` | Live counters per (op, tool) |
| `~/.claude/.retry_state.json.lock` | flock target |
| `~/.claude/.hook_health.json` | Heartbeat: pid + last_heartbeat ts |
| `~/.claude/.current_operation` | Operation namespace |
| `~/.claude/.retry_kill_switch` | v1.1 kill switch |
| `~/.claude/retry_policy.log` | Audit trail |
| `~/.claude/hooks/retry_policy_guard.sh` | PostToolUse enforcer |
| `~/.claude/hooks/userpromptsubmit_inject.sh` | Layer 0 Channel B injector |
| `~/.claude/hooks/_compile_policy.sh` | YAML → JSON + checksum |

## Cross-references

- [[operator-must-see-authenticated-actions]] — paired rule about operator-side visibility
- [[mcp-browser-must-be-visible]] — agent-side visibility precondition (the OBSERVABILITY_FAILURE source)
- [[attention-signal-tiers]] — speak.sh / TV / Jazz/Gospel for getting operator's attention when STOP fires
- Plan file: `~/.claude/plans/imperative-skipping-wadler.md` (full design, sources, verification tests)

## Status

**Schema version 1.2.** Updated 2026-05-23 incorporating 3 gaps from HF Ultraplan research (SHIELDA / AgentErrorTaxonomy / ReliabilityBench / Graph-Based Self-Healing). Changes from v1.1: (1) phase axis added to all 8 classes; (2) REFLECTION_FAILURE as 8th class with `operator_eyes → replan → operator_hands` fallback; (3) per-tool max_attempts overrides (browser_screenshot/eval/read → 2); (4) reliability metric roadmap for future recalibration. PostToolUse hook is the correct event — PostToolUseFailure does NOT exist in Claude Code. Persists across every session via auto-memory. If a future session retries a tool more than 3 times in one operation without applying fallback_order, that is a defect — escalate immediately.
