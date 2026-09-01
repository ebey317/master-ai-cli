# Master AI CLI (Sensei) — Roadmap to Hermes Agent Parity

Last updated: 2026-09-01

## Phase 0 — Baseline & Inventory

### Current self-test score

Baseline (2026-09-01, before Phase 1.1): `SCORE 95/100, PASS=17 WARN=2 FAIL=0`.
After Phase 1.1 (RUN+RUNTERM typed dispatch, same day): `SCORE 97/100, PASS=18 WARN=1 FAIL=0`.

One open WARN remaining:
- **Sandbox boundary** (Phase 1.2) — shell commands run unconfined on the user's own machine; no `unshare`/`prlimit`/capability-dropping.

~~Typed tool boundary~~ — closed for RUN+RUNTERM, see Phase 1.1 below (READ/CREATE/EDIT still text-dispatched, noted as a fast-follow there).

### Public surfaces audited (exist today)

| Module | Role |
|---|---|
| `router.py` | Route decision (chat/code/filesystem/vision/etc.) |
| `typed_actions.py` | Typed action envelope for RUN/RUNTERM/READ/CREATE/EDIT — audit-only currently |
| `hooks.py` | Pre/post event bus |
| `subagent_registry.py` | 6 builtins (code_reviewer, context_inspector, directive_simulator, file_finder, spend_reporter, test_runner) — synchronous single-dispatch only, not concurrent |
| `observability.py` | Route/model/audit/block/fallback summarization; powers `stats` and Pupil `/metrics` |
| `skill_runtime.py` | STEPS-based skill executor; 1 skill built (`google-workspace`, verified live) |
| `master_ai_scheduler.py` | Cron daemon — restored + fixed 2026-09-01 (see below) |
| `sensei_bridge` (MCP) | 35 tools, actively consumed by Hermes itself |
| `pupil.html` + `stt_server.py` | Real running Web UI (`master-ai-ui.service`, port 8080, installable PWA) |

### Gap matrix vs. Hermes Agent's actual feature set

Full grounded comparison (checked against both codebases, not marketing copy) lives in `~/MD/handoff_sensei_hermes_parity_2026-08-31.md`. Summary:

| Feature | Status |
|---|---|
| Persistent Memory | Partial |
| Learning Loop (self-improving) | Gap |
| Multi-Platform Gateway (Telegram/Discord/Slack/WhatsApp/Signal) | Gap (dead vendored Discord code in `completion.py`, never wired) |
| Cron Scheduling | **Fixed 2026-09-01** — was built, silently broken, removed as "unused," now restored + actually working (see below) |
| Isolated/Concurrent Subagents | Partial — registry exists, dispatch is synchronous |
| Browser Automation | Parity/better |
| Voice Mode (STT/TTS) | Partial — TTS hardcoded to one static voice |
| Code Execution | Parity |
| Tirith-style Security (approval/permission layer) | Parity/deeper |
| Skill Marketplace (browse/install/audit) | Gap |
| Reinforcement Learning | Gap |
| Model Switching | Parity/better (10+ providers wired) |
| Tool Use (MCP) | Parity — sensei itself is an MCP server Hermes consumes |
| Local LLM Support | Parity |
| Web UI | Parity (Pupil) |
| Native macOS/Windows desktop clients | Gap — Linux-only |

### Chosen scope (for now)

Prioritizing surfaces that matter for Sensei's actual daily use: CLI/TUI, the sensei MCP bridge, the headless daemon/cron path, and Pupil (already-real web surface). Electron desktop and a full messaging gateway are explicitly deferred — see Phase 3.6/3.7 below.

---

## Cron Scheduling — closed out 2026-09-01

Two independent bugs found and fixed via actual end-to-end testing (not just code reading):

1. **Dead execution path.** The daemon's `_run_command()` shelled out to `master_ai.py --run <cmd>` — a flag that never existed. Headless mode was reworked to `--task`/`--headless` two days after the daemon was built (commit `11ebf5d`) and the daemon was never updated to match. Fixed: execute scheduled commands as real shell commands directly (matches how Hermes's own cron handles scripted jobs in `~/.hermes/cron/jobs.json`).
2. **Unreachable fire condition.** `_next_time()` computes "the next occurrence strictly after the given reference time" — but the loop was calling it with `now` as the reference and then checking `now >= result`, which can never be true by construction. Fixed: call it with the schedule's `last_run` (or creation time, if never run) as the anchor instead of `now`. Verified against 5 edge cases (new schedule not-yet-due, boundary reached, no double-fire same day, fires again next day, hourly mid-hour) plus a live E2E test with a real ~40s-out schedule that fired correctly.

Restored: `/schedule <cmd> HH:MM <hourly|daily|weekly|monthly>`, `/schedules`, `schedule start|stop|remove <id>` inside `master_ai.py`'s main() loop, plus the command-palette rows. Commit: (pending, see task list).

---

## Phase 1 — Tier-1 Hardening (Execution Safety)

### 1.1 Promote Typed Dispatch to Live Path

**RUN + RUNTERM done (2026-09-01).** The standards check for this was literally a hardcoded `add("WARN", ...)` with no test behind it — not a real gap measurement. Traced every execution path: RUN funnels through exactly one choke-point (`run_command()`), RUNTERM through exactly one (`run_in_terminal()`) — both now construct a real `TypedAction` at entry and finalize its lifecycle (`EXECUTING` → `COMPLETED`/`FAILED`) on every invocation, persisted to `AUDIT_LOG_JSONL` and a new bounded `_LAST_LIVE_TYPED_ACTIONS` global. Zero changes to any existing approval/blocking decision logic — purely additive instrumentation around the two already-battle-tested legacy dispatchers (`confirm_run`, `confirm_runterm`), so no regression risk to daily-driver behavior. The standards check itself now does a live probe (calls `run_command("true")`, asserts the resulting typed record) instead of an unconditional WARN. Score: 95 → 97/100.

Verified: new `test_typed_dispatch_e2e.py` (8 tests, real subprocess execution for RUN, mocked for the RUNTERM/timeout edge cases), existing `test_typed_actions.py` and `test_master_ai_parser.py` unaffected (same pre-existing unrelated failures, updated the one regression test that pinned the old WARN state).

**Fast-follow, not yet done:** READ (inline in `process_reply()`, no dedicated function) and CREATE/EDIT (`confirm_create`/`confirm_edit` each duplicate the file-write inline in two branches, no shared choke-point) need a small extraction first before they can get the same treatment.

### 1.2 Real Sandbox Boundary
- Wrap every shell dispatch: `timeout 60s prlimit --nproc=100 --nofile=256 --data=512M unshare -U -m -i -p -n bash -c "cd $WORK_DIR; $CMD"`.
- Drop Linux capabilities on a dedicated runner (`/opt/sensei-jail-runner` or a small C wrapper).
- Bind-mount `~/.ssh`, `~/.aws`, `~/.master_ai_keys` read-only/hidden.
- Non-sandbox override flag for operations that legitimately need full access, gated on explicit approval.
- Test: `test_sandbox_escape.py` — fork-bomb, privesc, symlink escape all blocked.

### 1.3 Read Path Fence + TTL
- Wire TTL check into every read gate in `_read_path_ok`.
- Default TTL 300s per approval; bind to identity hash + cwd; auto-revoke expired entries.
- Test: `test_secret_fence.py` — model can't read `~/.ssh`; approval expires and re-asks.

---

## Phase 2 — Tier-2 Hardening (Resource & Trust)

**Already satisfied — checked 2026-09-01, not open work.** This phase (and this whole roadmap's Phase 2 section) was drafted from the stale 2026-06-14 CLAUDE.md snapshot. The live self-test today shows both items already PASS: "output caps" (READ slice capped 8000 chars/file, tool RESULT capped 12000 chars in `_format_tool_result`) and "approval expiry" (`is_approved()` honors TTL + cwd scope, 24h default, legacy bare lines preserved for back-compat) both landed in a session between 06-14 and now that the notes never got updated to reflect. Nothing to build here.

### 2.1 Output Caps — done
### 2.2 Approval Expiry on All Trust Gates — done

### 2.3 Self-Test Gate
- Run `bash ~/scripts/sensei_selftest.sh` / check `agent_standards_score()`.
- Current: 97/100, 1 WARN (sandbox boundary, Phase 1.2), 0 FAIL. The "105/100" target in the original draft assumed both Tier-1 WARNs plus headroom beyond 100 — not literally reachable since the score is capped at 100; treat "0 WARN, 0 FAIL, 100/100" as the real target once Phase 1.2 lands.

---

## Phase 3 — Framework Expansion: Hermes Parity

### 3.1 Provider/Model Abstraction Layer
- `providers.py`: loads keys from `~/.master_ai_keys` by provider, tracks per-provider latency/error rates, implements fallback chain with retry/backoff. Supports Ollama, Groq, OpenRouter, Anthropic, Gemini, Fireworks, Cerebras.
- Moves CLAF from a simple router to a Hermes-style provider-agnostic engine.

### 3.2 Profiles & Isolation
- Finish `~/.master_ai_profiles/<name>/`: per-profile chats, tasks, memory, cache, permissions. Shared keys only where flagged.
- Add `sensei --profile <name>`.

### 3.3 Persistent Memory & Skills
- Skills not currently discoverable/loadable at runtime the way Hermes skills are.
- Build a `skills/` directory with `SKILL.md` frontmatter, auto-load on startup.
- Add `/skill list`, `/skill load <name>`, `/skill save <name>`.
- Make memory entries searchable and auto-summarized.
- **2026-09-01 note (Elijah):** not just self-authoring — Sensei should be able to *use and adapt* skill libraries that already exist elsewhere (e.g. Hermes's own `~/.hermes/skills/` tree, which already has 100+ authored skills) rather than only building its own from a blank `skill_runtime.py`. Skill-authoring (the "learning loop" gap) is still the harder, separate goal; adaptation of existing libraries is the more immediately tractable piece.

---

**2026-09-01 note (Elijah):** he likes that this session drops into an explicit plan/confirm step (Claude Code's plan mode) before touching anything risky or with a lot of surface area, and wants an equivalent on Sensei's own side — a pause-and-confirm gate before Sensei makes serious/risky changes to itself, not just the existing per-command RUN/CREATE/EDIT confirm dialogs. Not scoped into any phase above yet; needs its own design pass (what counts as "serious," where the gate lives, how it differs from the existing auto-mode destructive-command pause).

### 3.4 MCP Server Catalog
- Catalog local MCP servers in `~/.master_ai_mcp/`.
- `mcp add`, `mcp list`, `mcp remove`, `mcp enable/disable`.
- Support stdio and SSE transports; validate tool schemas before exposing to the model.

### 3.5 Headless / Daemon Mode
- `sensei daemon` / `sensei --headless`: Unix socket or HTTP API, accepts jobs, returns job IDs, logs to `~/.master_ai_logs/`, optional webhook callbacks.
- Note: `headless_runner.py` already exists but its model-reply path is a placeholder stub (no real LLM wired in) — this phase needs to actually wire it to a model, not just reuse it as-is.

### 3.6 Lightweight Web Dashboard (Optional)
- Minimal Flask/FastAPI dashboard: chat replay, route/model stats from `observability.py`, task queue, approval queue (`approval_queue.py`), memory/skill browser. Local-only binding + token.

### 3.7 Messaging Gateway (Optional)
- Only if remote trigger capability is wanted. Start with Telegram bot using the Phase 3.5 daemon API.

---

## Phase 4 — Surfaces & UX

### 4.1 TUI Polish
- Split-pane layout (chat + side panel for tasks/memory/approvals), status bar (model/route/safety score), slash-command palette, live streaming tokens with syntax highlighting.

### 4.2 Theming
- `~/.master_ai_skins/` with YAML/JSON theme files, applied to TUI + web dashboard. Default dark theme matching current terminal look.

### 4.3 CLI Command Suite

| Command | Purpose |
|---|---|
| `sensei` | Interactive TUI |
| `sensei chat -q "..."` | One-shot query |
| `sensei --profile work` | Launch with profile |
| `sensei daemon` | Headless mode |
| `sensei model` | Model/provider picker |
| `sensei doctor` | Health check |
| `sensei config get/set` | Settings |
| `sensei mcp list` | MCP catalog |
| `sensei skill list` | Skills |
| `sensei stats` | Observability |
| `sensei task list` | Task queue |

---

## Phase 5 — Packaging & Distribution

- `install.sh`: set up `sensei`/`master` commands, default profile, required dirs (`~/.master_ai_profiles/default/`, `~/.master_ai_skills/`, `~/.master_ai_mcp/`, `~/.master_ai_logs/`, `~/.master_ai_skins/`), sandbox runner on Linux.
- Update `pack_for_sale.sh`; add a clean-machine install test.
- Document offline-first setup and optional cloud escalation.

---

## Phase 6 — Verification & Certification

Before calling it done:

```bash
python3 ~/scripts/test_typed_dispatch_e2e.py
python3 ~/scripts/test_sandbox_escape.py
python3 ~/scripts/test_secret_fence.py
python3 ~/scripts/test_output_caps.py
python3 ~/scripts/test_approval_expiry.py
bash ~/scripts/sensei_selftest.sh
```

1. `agent_standards_score()` → 105/100.
2. Clean install tested in a VM or container.
3. `sensei daemon` accepts and completes a job headlessly.
4. MCP server add/remove works.
5. Profile switch preserves isolation.

---

## Suggested Execution Order

1. Phase 1 first — typed dispatch + sandbox + read-fence TTL (Tier-1 blockers, unlock the 105 score).
2. Phase 2 — output caps + approval expiry.
3. Phase 3.1–3.3 — providers, profiles, skills/memory (Hermes-class flexibility).
4. Pick 3.5 or 3.6 next depending on headless API vs. web dashboard priority.
5. Save 3.7 messaging gateway for last — biggest operational burden.

Related: [[project-hermes-vs-claf-distinction]], `~/MD/handoff_sensei_hermes_parity_2026-08-31.md`, `~/MD/handoff_sensei_hermes_parity_2026-08-20.md`
