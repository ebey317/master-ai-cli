# Master AI Runtime Notes

Last updated: 2026-06-14

This repo is Elijah's local-first AI agent stack. It runs standalone on this machine and does not require Claude/Codex relay wiring for normal operation.

## Architecture: Where the Agent Loop Lives

**Master AI** (`ebey317/master-ai-private`) = **THE AGENT LOOP** (Sensei)
- `master_ai.py`: Core loop (`handle()` → `process_reply()` → dispatch)
- `sensei_tui.py`: Terminal UI
- Runs the continuous agent cycle in real-time on the user's machine
- High-rate task execution, offline by default

**CLAF** (`ebey317/claf`) = **THE ROUTER** (decision layer)
- `orchestrator.py`: FastAPI proxy (not an agent loop)
- `claf_config.py`: Provider selection logic (local vs. cloud)
- `task_state.py`: Persistent task file handling
- Delegates requests to Ollama (local) or cloud providers
- Routes decisions, doesn't run the agent loop itself

**The Flow:**
```
Master AI (loop)
  ↓ request
CLAF (router) ← decides local vs. cloud
  ↓ delegates to
Ollama / Cloud (models)
  ↓ response
Master AI (continues loop)
```

**Key distinction:** Master AI is Sensei. CLAF is the brain-swap that lets Sensei run Claude Code's UX but with local models by default.

---

## 2026-06-14 — 95→105 Execution Hardening Roadmap

**Goal: Close the 5 WARNs to hit 105/100 and make the world rely on this offline-only.**

Elijah has built "the stick" — the foundational off-grid-first architecture. The gap from 95→105 is not new concepts; it's hardening execution so the stick can't be bent. Each WARN is a real failure mode. Fix these in priority order; do NOT certify as world-ready until all 5 are PASS.

### TIER-1 BLOCKING: Typed Tool Dispatch (Execution Safety)

**Current:** `process_reply()` regex-parses free model text → dispatches directly. Cloud models hallucinate success when safeguards block.  
**WARN:** Typed dispatch is shadow-parse audit-only, not live. `typed_actions.parse_reply()` exists but `process_reply` still uses legacy regex buckets.  
**Failure mode:** Model claims "done" when blocked. User loses work.

**To PASS:**
1. Move `typed_actions.parse_reply()` from shadow audit to live dispatch path.
2. Add schema validation for every action type:
   - `RUN` → command string, not arbitrary text
   - `RUNTERM` → shell syntax check before dispatch
   - `READ` → path + fence check before execution
   - `CREATE` / `EDIT` → syntax verification (Python/shell/JSON) before write
3. On validation fail, set `_LAST_BLOCKED_ACTION` and feed back to history as `[TOOL BLOCKED]` (don't silently fail).
4. Add end-to-end test: `test_typed_dispatch_e2e.py` → all model outputs validate before ANY dispatch.

**Code owners:** Kimi (dispatch layer), Claude (schema validation + audit).

**Verification:**
```bash
python3 ~/scripts/test_typed_dispatch_e2e.py
# Expected: every model output parses + validates; failed actions blocked with reason
```

---

### TIER-1 BLOCKING: Sandbox Boundary (Isolation Security)

**Current:** Shell commands run directly on user machine. No resource limits, no capability dropping, no filesystem escapes.  
**WARN:** `shell command` is unconfined. A model-induced loop can fork-bomb, fill disk, steal SSH keys.  
**Failure mode:** `for((;;));do true;done` runs unstopped → disk full → system hang. Runaway subprocess reads `~/.aws/credentials`.

**To PASS:**
1. Wrap every shell dispatch in `unshare` + `prlimit`:
   ```bash
   timeout 60s \
     prlimit --nproc=100 --nofile=256 --data=512M \
       unshare -U -m -i -p -n \
         bash -c "cd $WORK_DIR; $CMD"
   ```
2. Drop Linux capabilities (no root even if SUID):
   ```bash
   setcap cap_sys_chroot,cap_sys_admin-ep /opt/sensei-jail-runner
   ```
3. Bind-mount sensitive paths read-only in the jail:
   - `~/.ssh` → read-only (no private key theft)
   - `~/.aws` → read-only (no credential leak)
   - `~/.master_ai_keys` → hidden (no API key theft)
4. Add test: `test_sandbox_escape.py` → verify fork-bomb, privesc, fs escape all blocked.

**Code owners:** Claude (jail wrapper), Kimi (test suite).

**Verification:**
```bash
python3 ~/scripts/test_sandbox_escape.py
# Expected: fork-bomb exits at prlimit; privesc fails; symlink escape blocked
```

---

### TIER-1 BLOCKING: Read Path Fence + TTL (Secret Leak Prevention)

**Current:** `_read_path_ok` blocks symlink escapes. Approval entries have TTL + cwd. But old approvals can still grant permanent access.  
**WARN:** Approval expiry is in code; not wired into every read gate. Stale approvals don't auto-revoke.  
**Failure mode:** User approved `~/.ssh/config` read 2 hours ago. Model reads it again now without re-asking.

**To PASS:**
1. Wire TTL check into every read gate:
   ```python
   def _read_path_ok(path, cwd):
       key = (path, cwd)
       if key in _APPROVALS_WITH_TTL:
           entry = _APPROVALS_WITH_TTL[key]
           if time.time() - entry['approved_at'] > entry['ttl_seconds']:
               del _APPROVALS_WITH_TTL[key]  # Expired; re-gate
               return False, "approval expired"
       return _cwd_fence_ok(path)
   ```
2. Set TTL = 300s (5 min) per approval. After idle, re-ask.
3. Bind approval to identity hash + cwd to prevent escalation across sessions.
4. Add test: `test_secret_fence.py` → verify old approvals expire; re-ask fires after TTL.

**Code owners:** Claude (TTL + expiry), Kimi (test coverage).

**Verification:**
```bash
python3 ~/scripts/test_secret_fence.py
# Expected: model can't read ~/.ssh; approval expires after 300s; re-ask fires
```

---

### TIER-2: Output Caps (Resource Exhaustion Prevention)

**Current:** Model can emit unlimited tokens. No output size limit.  
**WARN:** Runaway loop → 10GB output file → disk full → system hangs.  
**Failure mode:** Model's reasoning loop emits 100GB of text. Disk fills. System becomes unresponsive.

**To PASS:**
1. Cap output per turn (50MB default):
   ```python
   OUTPUT_CAP_BYTES = 50 * 1024 * 1024
   _output_bytes_this_turn = 0
   
   def _safe_emit(text):
       global _output_bytes_this_turn
       _output_bytes_this_turn += len(text.encode('utf-8'))
       if _output_bytes_this_turn > OUTPUT_CAP_BYTES:
           _SENSEI_APP.emit_error(f"[OUTPUT CAP HIT] Max {OUTPUT_CAP_BYTES} bytes")
           return False
       _SENSEI_APP.emit(text)
       return True
   ```
2. Reset cap at turn boundary.
3. Log cap hits to audit trail.

**Code owners:** Claude (wrapper), Kimi (logging).

**Verification:**
```bash
echo "$(python3 -c 'print(\"x\" * 100000000)')" | sensei "read this"
# Expected: [OUTPUT CAP HIT]; disk safe; no 100GB file
```

---

### TIER-2: Approval Expiry (Time-Scoped Trust)

**Current:** `_read_path_ok` has TTL in code. But `_SELF_MOD_DENYLIST` is forever.  
**WARN:** Denylist entries never expire. Old block-outs persist indefinitely.  
**Failure mode:** Old denylist entry blocks a future legitimate edit that user now wants to allow.

**To PASS:**
1. Add expiry to all trust gates (not just reads):
   ```python
   class ApprovalEntry:
       def __init__(self, kind, value, ttl_seconds=300):
           self.kind = kind  # 'read' | 'edit' | 'run_terminal'
           self.value = value  # path or command
           self.created_at = time.time()
           self.ttl = ttl_seconds
       
       def is_expired(self):
           return time.time() - self.created_at > self.ttl
   ```
2. Check expiry on every approval check; re-ask if expired.
3. Wire into: read gates, edit gates, terminal gates, shell approvals.

**Code owners:** Claude (expiry logic), Kimi (gate wiring).

**Verification:**
```bash
sensei "read /home/user/file.txt"    # → ask for approval
sleep 301                             # 5 min + 1 sec
sensei "read /home/user/file.txt"    # → ask AGAIN (TTL expired)
# Expected: same user, same file, two separate approval asks
```

---

### Verification Checklist for 105/100

Run these tests before declaring world-ready:

```bash
# Full test suite
python3 ~/scripts/test_typed_dispatch_e2e.py        # Execution safety
python3 ~/scripts/test_sandbox_escape.py            # Sandbox boundary
python3 ~/scripts/test_secret_fence.py              # Read path fence
python3 ~/scripts/test_output_caps.py               # Output caps
python3 ~/scripts/test_approval_expiry.py           # Approval expiry

# Integration gate
bash ~/scripts/sensei_selftest.sh                    # Phase 16 & 17
# Expected: 0 FAIL, 0 WARN, agent_standards_score() → 105/100

# Customer validation
bash ~/scripts/pack_for_sale.sh /tmp/test-pack
cd /tmp/test-pack && bash INSTALL_FIRST.sh           # Clean machine install
master                                               # Open + run offline
# Expected: boots, routes locally by default, no cloud required
```

---

### Why These Five Are Not Optional

| Gap | Failure Mode | Impact | 105-Fix |
|-----|--------------|--------|---------|
| **Typed boundary** | Cloud model hallucinates success; user loses work | Data loss | Schema validation pre-dispatch |
| **Sandbox** | Fork-bomb, privesc, key theft | System compromise | prlimit + capabilities + unshare |
| **Read fence + TTL** | Stale approvals grant permanent secret access | Secret leak | TTL + re-gate on expiry |
| **Output caps** | 100GB output → disk full → hang | System hang | Per-turn byte cap + graceful stop |
| **Approval expiry** | Old blocks prevent future legitimate edits | False negatives | Expiry on all trust gates |

---

### Strategic Notes

- **Elijah built the stick.** Off-grid-first architecture is the foundation. These five fixes harden execution so the stick **can't be bent**.
- **"Off-grid is the architecture. Everything else is convenience."** Cloud fallback is still there. But the default routing, tool gates, and isolation **must be bulletproof offline**.
- **Do NOT claim "world-ready" until all 5 are PASS.** Current score 95/100 is honest. Stay honest until evidence, not vibes.
- **No more "toy" agent.** These fixes transform Sensei from "neat demo" to "production-grade: typesafe, isolated, auditable, TTL-scoped."

---

### Handoff to Claude + Kimi

**Claude lane:** Audit schema validation + output caps + TTL expiry. Provide typed-safe schemas for each action type. Challenge any dispatch that skips validation.

**Kimi lane:** Wire typed dispatch live. Implement sandbox wrappers. Add end-to-end tests. Keep the five PASS items all green in Phase 16.

**Next session:** Pick one TIER-1 blocker. Land it end-to-end (code + test + audit). Move to the next. Do not skip to TIER-2 until both TIER-1 items are PASS.

---

## 2026-05-11 — P0-P2 roadmap landed

Claude handoff claimed 13 commits, but local `git log` shows 11 P0-P2 commits on top of `22f2e21`. Do not invent missing hashes; if two were squashed, recover from reflog before citing them. The verified local stack is:

| Commit | Summary | Notes |
|--------|---------|-------|
| `8919d38` | Pupil HTTP API contract | `stt_server.py` exposes `/health`, `/status`, `/chat`, `/events`, `/mode`, `/voice`; `pupil_api.md` and `test_pupil_api.py` define the contract. |
| `c1e282a` | Router boundary + golden tests | New `router.route()` / `RouteDecision`; golden tests cover chat/code/filesystem/current events/vision/terminal visual/reasoning/system-query/weather/messy voice; vision negation + system-query harvest recording landed. |
| `b9ccc2c` | Typed action envelope + jsonl audit | New public surface `typed_actions.TypedAction`; `typed_actions.parse_reply()` and `~/.master_ai_audit_typed.jsonl` provide structured audit records, but executor dispatch still uses legacy regex buckets. |
| `56e07ca` | Adaptive slicer | Existing symbol slicer now scales context by density and intent; preserves `_extract_target_symbols` / `_slice_around_symbol` path. |
| `2376cca` | Per-route history budgets | Route-specific prompt budgets reduce local-context drag without changing public prompts. |
| `c7586d4` | Reasoning surface | Adds `reason fast|standard|deep|max` / `reason:` path over `sensei_reasoning_loop.run_reasoning_loop()`. Outputs must stay inert prose, not executable directives. |
| `22bf7aa` | Hooks system | New public surface `hooks.fire()` plus config-driven pre/post events. Hook blocks feed `[HOOK BLOCKED]` back into history. |
| `ca3f813` | Coding task loop | Enforces READ before EDIT and adds syntax verification paths for edited shell/python content. |
| `3f695ee` | Observability dashboard | New public surface `observability.summarize()`; `stats` command and Pupil `/metrics` summarize route/model/audit/block/fallback data. |
| `579174a` | Subagent registry + 6 builtins | New public surface `subagent_registry.run()`; builtins include `code_reviewer`, `context_inspector`, `directive_simulator`, `file_finder`, `spend_reporter`, `test_runner`. Subagent outputs are inert JSON, not executable replies. |
| `3257750` | Read fence + approval TTL/cwd | `_read_path_ok` blocks secret paths/symlink escapes; approval entries have `ts`, `cwd`, and TTL while legacy bare approvals remain compatibility entries. |

### Public surfaces added

- `router.route(history, user_text, image_path=None)` returns a normalized route decision and keeps `master_ai.orchestrate()` behind a small importable boundary.
- `typed_actions.TypedAction` is the internal action envelope for `RUN`, `RUNTERM`, `READ`, `CREATE`, `EDIT`; current usage is audit/preview, not full dispatch.
- `hooks.fire(event, action, ...)` is the event hook bus for pre/post tool behavior.
- `subagent_registry.run(name, task, context=None)` dispatches typed specialized agents and returns inert structured data.
- `observability.summarize(limit=500)` powers raw CLI `stats` and Pupil `/metrics`.

### Verification notes

- `bash ~/scripts/sensei_selftest.sh` passed: 110 PASS, 0 WARN, 0 FAIL.
- `agent_standards_score()` now returns 95; `format_agent_standards()` reports PASS=17 WARN=2 FAIL=0.
- Remaining WARNs stay honest: `typed tool boundary` remains WARN because `process_reply()` still regex-parses free model text before dispatch; `sandbox boundary` remains WARN because shell commands run unconfined.
- Do not call this 100/100 or Anthropic-certified until typed dispatch is end-to-end and real sandboxing exists.
- Dirty working-tree comments in parser/safety tests may still describe the pre-P2.2 WARN set; runtime truth is the standards report, not those stale comments.

---

## Current Positioning

Master AI is a local-first coding and computer-control agent for the user's own machine.

- Sensei: tmux terminal agent in `master_ai.py` (THE AGENT LOOP)
- Pupil: browser UI in `pupil.html`
- Dojo: optional project/task picker, not an entry gate
- Local default: Ollama models (via CLAF router)
- Cloud escalation: Groq for fast replies, DeepSeek-R1/OpenRouter for reasoning, Gemini/web for live facts

Use this wording when describing it:

> Local-first computer agent with optional cloud escalation.

---

## Important Existing Architecture

- `harvest.py` is already the reuse layer.
  - Records local and cloud calls.
  - Serves near-duplicates from cache with no model call.
  - Provides few-shot examples.

- Cloud identity is already injected.
  - Cloud calls should understand "you / your app / this project" as Master AI itself.

- Do not reintroduce a mandatory Dojo gate.
  - Project pinning is useful, but Sensei should open immediately.

- Do not treat cloud lanes as agents.
  - They are router destinations.

- Keep terminal entry behavior simple:
  - `master` = one-command portal/menu.
  - `sensei` = direct local Claude Code-style terminal agent.
  - Buyer installer should set this up automatically.

## Tests / Gates

Run before declaring ready:

```bash
python3 -m py_compile ~/scripts/master_ai.py ~/scripts/harvest.py
python3 ~/scripts/test_master_ai_parser.py
bash -n ~/scripts/master.sh ~/scripts/install.sh ~/scripts/pack_for_sale.sh ~/scripts/sensei_selftest.sh
bash ~/scripts/pack_for_sale.sh /tmp/master-ai-sale-test
```

Expected pack result: YELLOW self-test can pass if warnings are environment edges.
