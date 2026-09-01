# Master AI CLI (Sensei) — Roadmap to Hermes Agent Parity

Last updated: 2026-09-01 (Phase 3.5 marked closed — code/tests/commit were already done and live, only the ROADMAP write-up was still open when the machine's crash-reboot interrupted the session)

## Phase 0 — Baseline & Inventory

### Current self-test score

Baseline (2026-09-01, before Phase 1.1): `SCORE 95/100, PASS=17 WARN=2 FAIL=0`.
After Phase 1.1 (RUN+RUNTERM typed dispatch): `SCORE 97/100, PASS=18 WARN=1 FAIL=0`.
After Phase 1.2 (RUN sandbox boundary, same day): **`SCORE 100/100, PASS=19 WARN=0 FAIL=0`.**

~~Typed tool boundary~~ — closed for RUN+RUNTERM, see Phase 1.1 below (READ/CREATE/EDIT still text-dispatched, noted as a fast-follow there).
~~Sandbox boundary~~ — closed for RUN, see Phase 1.2 below (RUNTERM not sandboxed yet, noted as a fast-follow there).

Both former WARNs were hardcoded (`add("WARN", ...)` with no actual test), not real gap measurements — same pattern both times, caught and fixed the same way.

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
| Learning Loop (self-improving) | **Closed 2026-09-01** — Phase 3.3b, both halves done + re-verified |
| Multi-Platform Gateway (Telegram/Discord/Slack/WhatsApp/Signal) | Gap (dead vendored Discord code in `completion.py`, never wired) |
| Cron Scheduling | **Fixed 2026-09-01** — was built, silently broken, removed as "unused," now restored + actually working (see below) |
| Isolated/Concurrent Subagents | Partial — registry exists, dispatch is synchronous |
| Browser Automation | Parity/better |
| Voice Mode (STT/TTS) | Partial — TTS hardcoded to one static voice |
| Code Execution | Parity |
| Tirith-style Security (approval/permission layer) | Parity/deeper |
| Skill Marketplace (browse/install/audit) | **Closed 2026-09-01** — Phase 3.3b, both halves done + re-verified |
| Reinforcement Learning | Gap (deliberately deferred — Learning Loop above is supervised self-improvement, not autonomous policy training) |
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

**RUN done (2026-09-01).** Wraps `run_command()`'s actual `subprocess.run` in `_build_sandbox_argv()`: `systemd-run --user --scope -p TasksMax=200 -p MemoryMax=1G -- unshare -U -m -p --mount-proc --map-root-user -f -- prlimit --nofile=512 --as=1073741824 -- bash -c <hide-secrets-then-exec>`.

**Two deviations from the literal snippet above, both found by testing on this machine before implementing, not by following the snippet on faith:**
1. Dropped `-n` (network namespace). Tested first: puts the command in a namespace with zero connectivity, no veth/NAT set up. `~/.master_ai_audit.log` shows curl/wget/apt-cache/dpkg constantly in real daily use (weather checks, GitHub API lookups, package checks) — implementing `-n` as written would have silently broken most of what RUN is actually used for.
2. Dropped `prlimit --nproc=` for process-count limiting, use `systemd-run --user --scope -p TasksMax=` (cgroup v2 pids controller) instead. `RLIMIT_NPROC` (what `prlimit --nproc` sets) is accounted **per real UID system-wide**, not per process subtree — tested live: `prlimit --nproc=200 -- unshare ...` failed outright ("fork failed: Resource temporarily unavailable") even with only ~108 processes existing for the user; it only started working north of 500. That's not fork-bomb containment, that's a limit that could break Elijah's actual desktop the moment he has more than a couple things open (which he already does most days — this is the exact resource-contention pattern behind tonight's power-cycle). `systemd-run`'s cgroup `TasksMax` genuinely scopes to just the command's own subtree: verified live, a real fork bomb under `TasksMax=50` only moved the system-wide process count from ~108 to ~161 (contained inside its own cgroup), not an unbounded climb. `prlimit --nofile=`/`--as=` stay, since unlike `NPROC` those are per-process, not pooled — no equivalent risk.

Secret paths hidden by bind-mounting over their **resolved real path** (`readlink -f`, so the `~/.master_ai_keys` symlink → `~/Desktop/Projects/keychain/master_ai_keys` is hidden at its real location) — a directory gets an empty `tmpfs` overlay, a file gets bind-mounted over `/dev/null`. Verified live against real paths on this box (not synthetic fixtures): `~/.master_ai_keys` reads as 0 bytes from inside vs. 2825 bytes outside; `~/.ssh` (which genuinely has a real `id_ed25519` private key on this machine) reads as empty (2 entries = `.`/`..`) from inside vs. 6 real entries outside.

`sudo` commands never reach `run_command()` at all (`confirm_run()` routes them to `_sudo_handoff()` first) so sudo elevation is untouched by this. No capability-dropping / dedicated C runner binary built — the `unshare -U --map-root-user` approach achieves the same "no real root even if something tries" property without needing a separate setuid binary, which is simpler and was already sufficient for the threat model (fork bomb / memory runaway / secret read), not "prevent a determined privilege-escalation exploit."

Verified: new `test_sandbox_escape.py` (8 tests: real network access preserved, real fork-bomb containment measured via system-wide process count, real secret-path hiding against the actual `~/.ssh`/`~/.master_ai_keys` on this box, `.sh`-script targets still work, standards-check probe). Score: 97 → **100/100**, 0 WARN.

**Fast-follow, not yet done:** RUNTERM (`run_in_terminal()`) isn't sandboxed — it opens a real visible terminal window Elijah watches interactively; PID/mount-namespacing a GUI-spawned session is a different problem (terminal emulators fork detached) that deserves its own pass.

### 1.3 Read Path Fence + TTL — verified 2026-09-01, no gap to close

Checked directly (not assumed from the standards-check PASS): `_read_path_ok()` has exactly one call site (in `process_reply()`'s READ handling) and zero concept of approval or TTL — it's a pure stateless fence, re-evaluated fresh on every single READ, no memory of prior decisions either way. There is no "approve this read, remember it" flow for READ to have a TTL on in the first place: secret paths (`.ssh`, `.gnupg`, `.aws/credentials`, `.master_ai_keys`, `.netrc`, `/etc/shadow` etc., `/root`, `/proc`, `/sys`) are hard-denied unconditionally with no override, and everything else under the allowed roots (`$HOME`, `/tmp`, `/var/log`) is allowed unconditionally with no re-confirmation needed. Same finding for CREATE/EDIT: no `is_approved()`/`save_approved()` usage anywhere for either — every write gets a fresh fence check (`_cwd_fence_ok`), no persisted approval memory.

`is_approved()`/`save_approved()` (the actual TTL system, 24h default, cwd-scoped) is correctly used in exactly the two places that need a "remember my choice" convenience: `confirm_run()`'s "Always" option and `confirm_runterm()`'s equivalent — both real, risky, repeatable actions where re-asking every time would be pure friction. Verified both call sites directly.

**Conclusion: the original Phase 1.3 wording ("wire TTL into every read gate," "approval expires and re-asks") describes an approval-based READ flow that doesn't exist in the current codebase — same pattern as the Phase 2 finding (roadmap drafted from stale historical notes, not current code).** Building a new TTL/approval system for READ specifically would be a regression, not a fix: it would reintroduce the exact "stale approval grants permanent access" risk the original ask was worried about, where today's stateless fence has none (nothing to go stale). Not scoping any code change here.

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

**2026-09-01 note (Elijah) + audit, closed same day:** "make sure my sensei framework is its own and not dependent upon hermes or you [Claude]/anthropic... we're just mimicking, cloning, copying, borrowing, using, and learning" — inspiration and forking-then-owning-the-copy is fine, a runtime dependency on another framework being installed is not. Audited every `.hermes`/`anthropic`/`claude` reference across master-ai-cli and all 4 adapted skills. **Anthropic/Claude: no violation** — `ask_cloud_anthropic()` is one of ~10 optional cloud-escalation tiers gated by an API key, a no-op with none configured, matching CLAF's own off-grid-first default. **Hermes: exactly one real violation, now fixed** — the `google-workspace` skill directly called a script living inside `~/.hermes/` and read OAuth credentials from there; if Hermes were removed, that capability would break entirely. Forked `google_api.py`/`setup.py` into the skill's own `scripts/` directory (only the `_hermes_home` path-resolution coupling needed changing — everything else was already framework-agnostic `google-auth` logic), copied the OAuth credentials alongside, re-verified the exact same live Gmail-search check still works using only the copied files. `skill_mirror.py` (a dev-time tool for browsing Hermes's skill library to decide what's worth adapting next) is explicitly left as-is — that's the "learning from" part, not a runtime dependency. Commit `f97c630`.

### 3.1 Provider/Model Abstraction Layer — mostly already done, health tracking closed 2026-09-01

Checked `~/projects/claf` directly (not a new `providers.py` on faith) — two of the three asks already existed and work:
- **Credential pools from `~/.master_ai_keys`**: real, per-provider, already in `orchestrator.py:62-139` (loads JSON or KEY=VALUE, maps to per-provider env vars, each cloud peer independently gated). The ask just never opened that file.
- **Fallback chain with retry**: real, already the exact shape asked for. `next_cloud_peer()` (`claf_config.py`) does a 429-aware tier-ordered walk, used by `orchestrator.py`'s dispatch loop precisely as its own docstring describes.
- **Per-provider health/latency tracking**: this one was genuinely missing — closed now. Added a lightweight in-memory circuit breaker (`record_provider_outcome`/`is_provider_healthy`, 3 consecutive failures → 30s cooldown, any success resets immediately) wired into `_pick_cloud_peer`/`next_cloud_peer`/`pick_cloud_peer` via one shared exclusion helper, fail-open if every peer looks unhealthy at once. New `test_provider_health.py` (8 cases). Commit `e976f21` in the `claf` repo (separate repo from this one).

No new `providers.py` module built — would have reimplemented two things that already work. Supports whichever providers are configured in `claf_config.py`'s `PROVIDERS` list already (currently OpenRouter free/paid tiers, DeepSeek, OpenAI, Gemini; Cerebras/Groq/Fireworks suspended pending billing/key fixes — unrelated to this session's work, pre-existing state).

### 3.2 Profiles & Isolation — closed 2026-09-01

Checked first: almost all of this already existed (`master_ai.py:174-229`) — `_pfile()` already correctly routed chats/tasks/memory/cache/approvals/permissions per profile, `KEYS_FILE` already explicitly shared across profiles by design. The one real gap: `_ACTIVE_PROFILE_FILE` was read but **never written anywhere** — a named profile could never actually be activated. Same "complete plumbing, missing faucet" shape as the cron fix earlier tonight.

Built: `--profile <name>`/`--profile=<name>` CLI flag (checked at module scope, before `main()`'s own argv handling — the profile-path constants are already frozen by the time `main()` runs), plus in-session `profile <name>`/`profiles` commands reusing `refresh`'s existing `os.execvp` restart rather than inventing live-reload. Verified for real: a brand-new profile is genuinely isolated (wrote to one profile's memory, confirmed a different new profile can't see it), `KEYS_FILE` stays identical regardless of active profile, and the passive safety path (stale pointer to a deleted profile) still falls back to default correctly. Commit `5a620cb`.

### 3.3 Persistent Memory & Skills
- Skills not currently discoverable/loadable at runtime the way Hermes skills are.
- Build a `skills/` directory with `SKILL.md` frontmatter, auto-load on startup.
- Add `/skill list`, `/skill load <name>`, `/skill save <name>`.
- Make memory entries searchable and auto-summarized.
- **2026-09-01 note (Elijah):** not just self-authoring — Sensei should be able to *use and adapt* skill libraries that already exist elsewhere (e.g. Hermes's own `~/.hermes/skills/` tree, which already has 100+ authored skills) rather than only building its own from a blank `skill_runtime.py`. Skill-authoring (the "learning loop" gap) is still the harder, separate goal; adaptation of existing libraries is the more immediately tractable piece.
- **2026-09-01 progress:** 4 skills now adapted and working — `google-workspace` (2026-08-29, still verified live today), plus `web-search-ddgr`, `codebase-inspection`, and `systematic-debugging` (adapted 2026-08-31 via a Hermes delegation, independently re-verified end-to-end rather than trusting the delegation's own "verified" claim). Skill-authoring itself is still not built.
- **2026-09-01 research:** checked whether skill formats are actually portable across agents rather than assuming. They largely are at the spec level — pulled a real skill from `openclaw/agent-skills` (OpenClaw is a real, active open-source agent framework, `github.com/openclaw`, confirmed via direct API check) and its `SKILL.md` uses the exact same `name`/`description` YAML-frontmatter shape as Hermes's skills and Claude's own Skills. What's *not* portable is Sensei's execution model: `skill_runtime.py` deliberately requires a typed Python `STEPS` state machine rather than letting a general LLM improvise tool calls from the markdown directly (a safety tradeoff, not a limitation) — that's why each skill needs hand-adaptation rather than a drop-in copy. Treat "universal skill format" as already true at the spec level; treat "drop-in skill execution" as a real, larger undertaking if ever pursued, not a quick win.
- **2026-09-01 fix (same day, before anything else):** found and closed a real gap this raised — every adapted skill's `recipe.py` called `subprocess.run()` directly, completely bypassing the Phase 1.2 sandbox boundary. A skill run got zero fork-bomb containment, zero secret-hiding, zero typed audit trail, which matters a lot more once "pull skills from public repos" is the plan (third-party skills are less trusted than hand-adapted ones). Extracted the sandbox wrapper into a new dependency-free `sandbox.py` (both `master_ai.py` and every recipe import it independently — `skill_runtime.py` can't import `master_ai.py`, that would be circular) and updated all 5 real subprocess call sites across all 4 skills. Verified for real: secret-hiding still works through the extracted module, a real fork-bomb-shaped `repro_command` run through `systematic-debugging` left the system-wide process count completely flat (105→105) instead of climbing, and all 4 skills — including a real read-only Gmail search through `google-workspace` — still work end-to-end post-sandboxing. Commit `4083188`.

---

**2026-09-01 note (Elijah):** he likes that this session drops into an explicit plan/confirm step (Claude Code's plan mode) before touching anything risky or with a lot of surface area, and wants an equivalent on Sensei's own side — a pause-and-confirm gate before Sensei makes serious/risky changes to itself, not just the existing per-command RUN/CREATE/EDIT confirm dialogs. Not scoped into any phase above yet; needs its own design pass (what counts as "serious," where the gate lives, how it differs from the existing auto-mode destructive-command pause).

### 3.4 MCP Server Catalog — closed 2026-09-01

Delegated to Hermes (in parallel with 3.2, per Elijah's explicit request to split work instead of doing everything serially) and independently re-verified rather than trusted at face value — same discipline as the earlier TTS delegation. New `sensei_mcp_client.py`: Sensei can now act as an MCP *client* — discover/add/remove/enable/disable/validate other MCP servers, mirroring what Hermes does for itself via `hermes mcp`. Both stdio and SSE transports. Probe-before-trust is the core rule: nothing is enabled without a live `initialize`→`tools/list` round-trip passing schema validation; `add_server` probes before enabling, `set_enabled` re-probes and refuses if broken. New `mcp`/`mcp list`/`mcp add`/`mcp remove`/`mcp enable`/`mcp disable`/`mcp validate`/`mcp tools` commands.

Independently re-verified: re-ran the real smoke check (a real server, "sensei-self", registered with all 38 real tools validated), confirmed `py_compile` clean, confirmed the schema-validation rejection claim with real malformed test servers (both transports), and confirmed the full regression suite still passes. Commit `5a620cb`.

### 3.3b Skill Marketplace & Learning Loop — closed 2026-09-01

**Elijah's explicit priority call (2026-09-01):** of the remaining real
Gaps in the parity matrix above, **Skill Marketplace** and **Learning
Loop (self-improving)** go first — ahead of 3.5/3.6/3.7 below. Split
between Claude and Hermes the same way 3.4 (MCP catalog) was split:
Claude builds the two backing modules, Hermes wires the REPL surface and
independently-testable command flow, Claude re-verifies before closing.
Full task spec handed to Hermes:
`hermes_task_skill_marketplace_learning_loop.md` (scratchpad, addressed
directly to Hermes). Status: **both halves done and independently
re-verified 2026-09-01** — `skill browse/install/audit/improve` wired
into `master_ai.py`'s dispatch (Hermes), `confirm_edit()` gate confirmed
live to correctly refuse an auto-mode write, pytest baseline unchanged.
Committed `05524df`. Handoff: `~/MD/handoff_sensei_skill_marketplace_learning_loop_2026-09-01.md`.

**Claude's half — done 2026-09-01, verified live (backing modules,
stdlib-only, mirrors `sensei_mcp_client.py`'s probe-before-trust shape):**
- `skill_marketplace.py` — `~/.master_ai_skill_sources.json` catalog of
  skill *sources* (source #1: `~/.hermes/skills/`, already known-portable
  at the `SKILL.md` frontmatter level per the 3.3 portability research).
  `browse_source()` found all 151 real skills in the Hermes tree live
  (read-only listing, correctly flags the 4 already-adapted ones) and
  `audit_skill()`/`audit_adapted_skill()` (static-scans for the same
  sandbox-bypass bug class the 3.3 fix found — direct `subprocess`/
  `os.system`/`eval` calls that skip `sandbox.py`) proven live to hard-
  reject a deliberately broken test skill and pass/stage a clean one.
  Audit-only; never executes third-party code.
- `learning_loop.py` — read-only analysis over `skill_runtime.py`'s
  real per-session JSON records (`~/.master_ai_skills/<name>/sessions/
  *.json` — history/errors/current_step/done/aborted; richer than the
  skills' own `knowledge/*.jsonl`, which has no failure record at all).
  Ran live against all 25 real session files across the 4 adapted
  skills: real success/abort rates and step-level abort/retry patterns,
  including one organically-surfaced real quirk (a `google-workspace`
  session aborting at the `END` sentinel) flagged, not fixed — out of
  scope for this task. No auto-mutation anywhere in this module — it
  only produces a report.
  Handoff: `~/MD/handoff_sensei_skill_marketplace_learning_loop_2026-09-01.md`.

**Hermes' half (REPL wiring, exact `mcp`-command pattern):**
- `skill browse [source]`, `skill install <source> <name>` (audits
  first, refuses to stage anything that fails audit — same UX as `mcp
  enable` refusing a broken server; does NOT claim a staged skill is
  runnable, since format-portable ≠ execution-model-portable per the
  3.3 research), `skill audit <name>`.
- `skill improve <name>` — the actual self-improving loop, but
  human-gated on purpose: calls `learning_loop.analyze_skill()`, and for
  narrow rule-based fix opportunities only (not free-form model rewrites)
  drafts a diff that **must route through the existing typed EDIT
  confirm gate** (`confirm_edit()`, Phase 1.1) — no silent auto-write.
  This is the hard constraint carried over from tonight's sandbox work:
  the safety gate that Phase 1.2 built does not get bypassed just
  because the writer is "the skill improving itself."

**Explicitly deferred, staying a Gap on purpose:** true unsupervised
RL / auto-applied self-modification. What's being built here is
supervised self-improvement (analyze → propose → human/typed-gate
confirms → apply), not autonomous policy training — matches the existing
"Reinforcement Learning: Gap" row in the matrix, which stays open.

### 3.5 Headless / Daemon Mode — closed 2026-09-01

Split the same way as 3.3b/3.4/3.6: Hermes built the daemon (stdlib
`ThreadingHTTPServer`, house style per `master_ai_scheduler.py`), Claude's
parallel session landed the real model wiring in `headless_runner.py`
(the placeholder stub this phase existed to remove) while exercising the
daemon on port 8799 for its own testing — Hermes' later jobs already
answer with real model output as a result.

New `headless_daemon.py` (~650 lines): `start`/`status`/`submit`/`list`/
`job <id>`/`stop` CLI plus `POST /jobs`, `GET /jobs/<id>`, `GET /health`.
State in `~/.master_ai_jobs.json` (0600, atomic write), pid file, per-job
`.out`/`.err`/`.log` under `~/.master_ai_logs/`. Status machine
(pending→running→done|failed) enforced, not just recorded — illegal
transitions raise (caught a real duplicate-def bug this way before first
run). 15-minute hard cap per job, max 2 concurrent. Optional
`callback_url` webhook, best-effort, never crashes the daemon on failure.

**Startup reconciliation, found live not assumed:** the daemon was
killed mid-job during same-day parallel-session testing and jobs stayed
`running` forever. `reconcile_stale_jobs()` now runs at boot and marks
any pending/running jobs failed with "interrupted: daemon stopped or
restarted mid-job."

Verified for real: job lifecycle over the live HTTP API (pending→
running→done, exit=0), webhook proven against a local listener
(`job_1788293278_efa54d`, duration 164.13s, real model result), stale-job
reconciliation reproduced (4 jobs marked failed at next startup),
full CLI round-trip (`submit`/`job`/`list`/`status`/`stop`), `py_compile`
clean, pytest baseline unchanged (10 passed / 4 pre-existing failures,
zero new). Committed `05524df` (same commit as 3.3b — Hermes landed both
in one pass). Handoff: `~/MD/handoff_sensei_headless_daemon_2026-09-01.md`.

`headless_runner.py`, `master_ai.py`, `skill_runtime.py`, `sandbox.py`
untouched by the daemon build. Port pinned via `HEADLESS_DAEMON_PORT` env
var (both daemon and CLI honor it) so systemd units can avoid flag drift
if/when this gets its own unit.

### 3.6 Lightweight Web Dashboard — closed 2026-09-01
- Built as new panels on the existing pupil.html/stt_server.py surface,
  not a separate Flask/FastAPI app (the original "minimal dashboard"
  framing here was stale — pupil.html already existed as a real web UI).
  `dashboard_data.py` aggregates observability.summarize(), headless_daemon
  job queue, approval_queue, skill_marketplace + learning_loop, and
  ~/.master_ai_chats/*.chat replay into one GET /api/dashboard endpoint
  (Claude's half, commit `5a7d09e` + fix `f4fc727`); five read-only panels
  in pupil.html (Hermes' half). Local requests pass without auth (same
  trust as /chat); remote requests need X-Mesh-Token (same pattern as
  /ask). Split and re-verification followed the same Claude/Hermes
  discipline as 3.4 and 3.3b.
- **Deploy-drift finding, partially fixed:** `~/scripts/` had silently
  diverged from this repo on multiple files (stt_server.py, pupil.html,
  sensei_tui.py, dashboard_data.py, typed_actions.py) — some services run
  `~/scripts/*.py` directly rather than through the `master_ai.py`
  symlink, so repo commits never reached them without a manual copy.
  Converted the confirmed-safe files to symlinks matching the existing
  `master_ai.py` pattern (stt_server.py, pupil.html, sensei_tui.py,
  dashboard_data.py, headless_daemon.py, skill_marketplace.py,
  learning_loop.py, sandbox.py, observability.py, approval_queue.py,
  skill_improve_helpers.py) so this class of bug can't recur for them.
  **`typed_actions.py` deferred here, closed separately 2026-09-01
  (see below)** — the divergence turned out to be a real gap needing its
  own dedicated look, not a blind symlink call.

### `typed_actions.py` sync — closed 2026-09-01

The file 3.6 explicitly declined to touch turned out to have a real
history behind the drift. Plan-mode investigation traced it to a third
local clone, `~/projects/master-ai` (separate GitHub remote, shares
history with this repo up to a common-ancestor commit), which held a
genuine, already-committed, 13/13-verified fix (`68dcb57`, Elijah,
2026-08-26) that closed this exact PLAN/DONE/THINK/RUN_SKILL/SEND_EMAIL
gap — it had been hand-copied into `~/scripts/typed_actions.py` at the
time but never merged back into this repo, which is why the repo copy
stayed 601 lines while the live one grew to 845.

Ported `68dcb57`'s `typed_actions.py` + `test_typed_actions.py` hunks
into this repo via `git apply` (clean, additive-only, confirmed
byte-identical common ancestor first) and its `test_typed_dispatch_e2e.py`
under the new name `test_typed_actions_parity.py` (this repo's own
newer, unrelated file of the old name — live RUN/RUNTERM dispatch
instrumentation, `6493fd5` — was not touched). `~/scripts/typed_actions.py`
converted to a symlink into this repo, same pattern as the rest of 3.6
(backup kept: `typed_actions.py.bak.20260901_181722`). Verified: previously-
failing `test_directive_kinds_are_complete` now passes, new 13-test parity
suite green, existing 8-test e2e suite unaffected, full pytest baseline
unchanged (10 passed / 4 pre-existing failures, zero new),
`agent_standards_score()` confirmed 100/100. No live dispatch behavior
changed — the module's role is shadow-parse/audit only. Commit `9bfb3ba`.

**Open question surfaced, not resolved:** whether `~/master-ai-cli` and
`~/projects/master-ai` are meant to be reconciled as one project long-term
(each has real commits the other lacks — this repo has accumulated far
more since the shared ancestor, but `68dcb57` shows the reverse also
happens) or whether `~/projects/master-ai` should be considered retired.
This one-file port doesn't answer that; worth a decision at some point so
this class of silent fork doesn't recur elsewhere.

**Also noted, out of scope for this pass:** `~/scripts/test_master_ai_parser.py`
has its own small, unrelated drift from this repo's copy (this repo is
*ahead* there — a stale assertion about `_is_informational_cmd()` piping
behavior that predates an already-landed 2026-08-27/08-28 fix). And
`bash ~/scripts/sensei_selftest.sh` currently reports 4 FAIL/5 WARN —
confirmed unrelated to this work (checked `agent_standards_score()`
directly: 100/100, matches the documented Phase 1.2 baseline exactly).
The script's own hardcoded sanity-guard assertions (expecting
typed-tool-boundary/sandbox-boundary to still show WARN, i.e. written
*before* Phase 1.1/1.2 intentionally closed them) plus one unrelated
missing-file check (`sensei_behavior.md`) look stale and worth a look in
their own right, but touching that script wasn't part of this task.

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

1. ~~Phase 1 — typed dispatch + sandbox + read-fence TTL~~ — closed 2026-09-01.
2. ~~Phase 2 — output caps + approval expiry~~ — already satisfied, closed 2026-09-01.
3. ~~Phase 3.1–3.2, 3.4 — providers, profiles, MCP catalog~~ — closed 2026-09-01.
4. ~~Phase 3.3b — Skill Marketplace & Learning Loop~~ — closed 2026-09-01, both halves.
5. ~~Phase 3.5 — headless daemon~~ — closed 2026-09-01, both halves (daemon + model wiring).
6. ~~Phase 3.6 — web dashboard~~ — closed 2026-09-01, both halves.
7. ~~`typed_actions.py` vs. repo divergence~~ — closed 2026-09-01, see write-up above. Open follow-on (not scoped): whether `~/master-ai-cli` and `~/projects/master-ai` should be reconciled as one project.
8. Save 3.7 messaging gateway for last — biggest operational burden.

Related: [[project-hermes-vs-claf-distinction]], `~/MD/handoff_sensei_hermes_parity_2026-08-31.md`, `~/MD/handoff_sensei_hermes_parity_2026-08-20.md`

### Scroll fix (sensei_tui.py) — operator-confirmed 2026-09-01

Merge commit a83aff3 reconciled two independent click-to-focus/wheel-scroll
fixes and closed the mouse-event-swallow bug in both. **Elijah confirmed
live, in a real terminal with real mouse input, that scroll now works.**
Claude did not and could not independently verify this interactively
(headless session, no real mouse) — the code-level checks in a83aff3
(handler return values, Window fallback offset math) are necessary but
not sufficient proof; operator confirmation is what closes it.
