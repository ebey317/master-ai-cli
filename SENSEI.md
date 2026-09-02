# SENSEI.md — Master AI CLI / Sensei

This file replaces an earlier `AGENTS.md` copy that lived only in the stale
`~/projects/master-ai` clone (not this repo) and described a message-bus,
multi-process agent architecture with a `/metrics` observability system and
domain whitelisting. None of that exists in this codebase — verified by
direct grep on 2026-08-28: no message-bus protocol, no per-agent process
isolation, no domain whitelist. That file was aspirational/boilerplate, not
documentation. This one describes what is actually here.

## What this is

Single-process, terminal-based AI agent ("Sensei") that a user runs as their
daily-driver CLI. Local-first (Ollama) with optional cloud escalation
(OpenRouter, Groq, Gemini, etc.), MCP tool support, voice I/O, browser
automation via a companion Chrome extension. See `README.md` for the
product pitch and `CLAUDE.md` for the dated engineering history — this file
is the "how to work on it" doc, not a changelog.

## Identity framing

Sensei/Master AI CLI is **an MCP-capable agent whose hands are its wired
capabilities** — shell dispatch, the browser bridge, voice I/O, memory,
subagents, MCP tool calls — not "a coding assistant that also does other
stuff." Shell/code execution is one capability the cables happen to expose,
not the headline. It sits on the same shelf as any other infrastructure CLI
(git, docker, the user's other agent tooling), built to be routed through,
not marketed as a chatbot with extra tricks. This mirrors how Elijah frames
his own primary Claude Code identity (see his top-level `CLAUDE.md`: "MCP
client... cables ARE my hands... coding is one capability, not the
headline") — keep both docs consistent with that framing rather than
describing Sensei in coding-agent-first language. The "Closed-Loop Agent
Framework" name is referenced as the intended runtime shape for this kind
of agent but isn't separately detailed anywhere yet — don't invent detail
for it; treat it as a named-but-undocumented concept until it's fleshed
out.

## Live-wiring gotcha — read this before editing anything

This repo is not the only copy on disk, and it is not even fully
self-contained:

- **`master_ai.py`** — the file in this repo IS the live one. The user
  launches it via `sensei` → `~/.local/bin/sensei` →
  `~/scripts/launch_master_ai.sh` → tmux session `master-ai` →
  `python3 ~/scripts/master_ai.py`. **`~/scripts/master_ai.py` is a
  symlink to this repo's `master_ai.py`** (`readlink -f` to confirm). Edit
  tools will refuse to write through the symlink — edit the file in this
  repo directly, never `~/scripts/master_ai.py`.
- **`sensei_tui.py`** — the copy that actually renders the TUI is
  `~/scripts/sensei_tui.py`, a real standalone file, NOT this repo's copy
  and NOT a symlink to it. Python resolves `sys.path[0]` to the literal
  invoked script's directory (`~/scripts`), so `master_ai.py`'s
  `from sensei_tui import TUIStdout` always imports `~/scripts/sensei_tui.py`
  regardless of what this repo's copy says. **If you're debugging TUI
  behavior (input handling, rendering, key bindings), edit
  `~/scripts/sensei_tui.py`, not this repo's copy** — then keep this repo's
  copy in sync by hand, since nothing does it automatically.
- **Known divergent copies exist** at `~/scripts/`, `~/master-ai-cli/`
  (this repo), `~/projects/master-ai/`, `~/projects/master-ai-cli/` — for
  `master_ai.py` and `sensei_tui.py` at minimum, possibly others. An
  unresolved merge is visible in git status as `master_ai.py.bak.20260827`
  and `master_ai.py.conflicted` (untracked, left over — don't delete
  without checking with Elijah first, but don't treat them as current
  either).
- **The browser bridge and MCP server are NOT in this repo.** `sensei_bridge.py`
  (Flask, port 8791, bridges to the Chrome extension) and
  `sensei_mcp_server.py` (MCP tool exposure) live in `~/scripts/` — and yet
  another divergent copy of `sensei_mcp_server.py` runs live from
  `~/projects/master-ai/`. If a task touches browser automation, verify
  which copy is actually the running process (`pgrep -f sensei_mcp_server`)
  before editing.

**Bottom line:** before editing any file this project shares a name with
elsewhere on disk, confirm which copy is live (`ps aux | grep <name>`,
`readlink -f <path>`, or check the running process's `/proc/<pid>/cwd` and
open file handles) rather than assuming the repo copy is what's executing.

## Verifying a change actually landed

There is no hot-reload. After editing `master_ai.py` or the live
`sensei_tui.py`:

1. `python3 -m py_compile <file>` first — a syntax error kills the whole
   engine on restart, in the user's live session.
2. Send `kick` into the running `master-ai` tmux session
   (`tmux send-keys -t master-ai "kick" Enter`) — soft-restarts the engine
   via the supervisor loop (exit 42), reloading all modules. Full reset
   ("full session reset + engine restart") is `new` or `clear`, not `kick`.
3. Re-run the exact scenario that was broken, live, in that tmux pane
   (`tmux capture-pane -t master-ai -p -S -N`) — a passing unit test does
   not prove the live dispatch path is fixed; `process_reply()`'s regex
   parsing and the TUI's key handling are both real places a fix can pass
   in isolation but still not fire in the live loop.

## Test / build gates

```bash
python3 -m py_compile master_ai.py harvest.py
python3 test_master_ai_parser.py      # directive parsing / routing, ~71 tests
python3 test_master_ai_safety.py      # policy/sandbox/audit acceptance, 45 tests
bash -n master.sh install.sh pack_for_sale.sh sensei_selftest.sh
bash sensei_selftest.sh               # full phase gate incl. safety acceptance (Phase 16)
```

`test_master_ai_parser.py` has **6 pre-existing failures unrelated to
routine work** (confirmed via `git stash` A/B on 2026-08-28): dead
cloud-provider routing (fireworks/groq disabled 2026-08-27) and one route
that no longer matches current cloud lane behavior. A 7th test
(`test_cloud_lane_continues_run_read_then_synthesizes`) is flaky/order
dependent — appears and disappears across reruns with no code changes.
Don't chase these unless the task is specifically about cloud routing; do
compare failure *counts and names* before/after your change (`git stash`)
so you can tell your edit apart from this pre-existing noise.

## Real safety surfaces (grep-verified, 2026-08-28)

These are the actual gates in `master_ai.py` — not the fictional
whitelisted-domains/message-bus-ack story from the old stale AGENTS.md:

- `is_blocked(cmd)` — pattern-based hard blocks (pipe-to-shell installers,
  credential exfiltration, malware/persistence patterns, recursive
  chmod/chown on root, raw block-device writes).
- `_cleanup_safety_issue(cmd)` — blocks broad deletes touching protected
  paths (`~/Downloads`, `~/Documents`, etc.) or unscoped `find ~ ... -delete`.
- `_agent_policy_issue_for_request(text)` / `_agent_policy_issue_for_command(cmd)`
  — policy gate wired at both request-entry (`handle()`) and command-dispatch
  (`confirm_run`/`confirm_runterm`).
- `_cwd_fence_ok(filepath)` — blocks reads/writes to secret paths and a
  self-modification denylist (this file, `sensei_tui.py`, `install.sh`,
  `pack_for_sale.sh`, `sensei_selftest.sh`, key/config files) even inside an
  otherwise-allowlisted directory, in auto mode.
- `confirm_run(cmd)` / `confirm_runterm(cmd)` — the actual approval gates
  RUN/RUNTERM directives pass through; a refusal sets `_LAST_BLOCKED_ACTION`
  and feeds `[TOOL BLOCKED]` back into history so the model doesn't
  hallucinate success on the next turn.
- `_sudo_handoff(cmd)` — sudo never executes inside Sensei; hands off to a
  separate terminal the user controls, full stop.
- `agent_standards_score()` / `format_agent_standards()` — the honesty
  gate. Do not report this system as "Anthropic-grade" or "100%" without
  running this and getting evidence; it is designed to stay at WARN on the
  known-incomplete items below until they are actually fixed.

## Known, acknowledged architecture gaps — stay honest about these

- **Typed tool boundary: WARN.** `process_reply()` still regex-parses free
  model text into RUN/RUNTERM/READ/CREATE/EDIT directives and dispatches
  directly. `typed_actions.parse_reply()` exists as a shadow/audit path but
  is not the live dispatch path. A model that emits malformed or
  adversarial text can still slip past intent.
- **Sandbox boundary: WARN.** Shell commands run unconfined on the user's
  machine — no `unshare`/`prlimit`, no capability dropping, no read-only
  bind-mounts over `~/.ssh` or `~/.aws`. This is a single-user personal
  machine, not a multi-tenant service, which is why this has stayed
  deprioritized — but don't describe it as sandboxed.
- **Approval expiry / output caps: not fully wired.** TTL exists in some
  approval paths, not all; there is no per-turn output byte cap.
- **Subagents are in-process function calls, not isolated processes.**
  `subagent_registry.run()` calls `sa.run(task, context=context)`
  synchronously in the same Python process as the main engine — a crashing
  subagent is caught with try/except, but there is no real fault isolation.

## Fixed 2026-09-01 — turns ending on announcement, no answer

Live session on Mary: user said "proceed", Sensei replied "On it. 🔍" and
the turn ended — no directive dispatch, no follow-up, nothing. Happened
twice in one session; the user had to ask "why did you stop without giving
me an answer?" the first time. Root cause had two parts:

1. `~/.sensei_behavior.md` — referenced by `BEHAVIOR_FILE`/`load_behavior()`
   and by the top-of-file comment ("Full canonical profile ... loaded into
   Sensei's system prompt via ~/.sensei_behavior.md") since long before this
   fix — **never actually existed on either machine.** `load_behavior()`
   silently returns `""` on a missing file, so this had been a no-op the
   entire time with no error surfaced anywhere. Created it (see the file
   itself for content) with a "read, work, AND answer" completion contract.
2. Even a fully-populated `.sensei_behavior.md` wouldn't have reached the
   turn that actually failed — `_behavior_block` is conditioned on
   `not is_chat_fast`, and `cloud_fast` (Groq) is exactly the lane casual
   back-and-forth chat routes through. Added the same completion rule
   directly to the unconditional header text of both `LOCAL_SYSTEM` and
   `CLOUD_SYSTEM` in `master_ai.py` so it reaches every route, `cloud_fast`
   included.

Not independently verified against a live repro yet — the original failure
was intermittent/prompt-dependent, not a deterministic unit-testable
condition. If it recurs, that means the instruction alone isn't sufficient
and this needs an actual structural fix (e.g. detecting a directive-free,
short, intent-only reply and forcing a follow-up turn before returning
control to the user) rather than another prompt tweak.

## Open bugs (as of 2026-08-28, from a live stress-test session)

Full detail: `~/.claude/projects/-home-elijah/memory/project_sensei_cli_stress_test_2026-08-28.md`

- **Fixed:** bare (non-piped) `grep`/`egrep`/`fgrep`/`rg` exiting 1 used to
  hard-BLOCK; now correctly treated as informational "no match" like the
  piped case already was (`_is_web_grep_no_match` in `master_ai.py`).
- **Fixed:** `sensei_tui.py`'s Enter handler had a dead 50ms-Enter-collapse
  heuristic (superseded by a proper `Keys.BracketedPaste` handler the same
  day it was added, but never deleted) that silently merged/dropped rapid
  distinct commands. Removed.
- **Open:** a message starting with the literal word "read" (e.g. "read the
  thread and tell me what's going on") gets intercepted by the literal
  `read <path>` file-read shortcut instead of reaching the model.
- **Open:** "search google for X" can silently skip the real browser
  dispatch (no `/extension/queue` hit in `sensei_bridge.log`) and fall back
  to a low-quality Wikipedia/DuckDuckGo scrape with irrelevant results, with
  no visible indication to the user that it didn't use the real browser.
- **Open:** asking about the `doctor` built-in in some contexts causes the
  model to hallucinate running `python3 master_ai.py help` as a shell
  command — which recursively launches the entire engine as a child
  process and can hang indefinitely, blocking the whole session's input
  queue until manually killed.

## Working conventions

- **Verify the live execution path, not just the parser/unit-test route.**
  This codebase has burned this before — a fix landing in a shadow/audit
  path while the live dispatch path stays untouched. "Tests pass" is not
  "verified working"; restart the live `master-ai` tmux session and
  reproduce the original failure.
- **Don't claim a score or certification without running the actual gate**
  (`agent_standards_score`, `sensei_selftest.sh`) and citing the number.
- **Stay on task.** Don't bundle unrelated cleanup into a bug fix; this repo
  already carries a large uncommitted/dirty-tree history from doing that.
- **Never delete the multi-copy duplication** (`~/scripts`,
  `~/master-ai-cli`, `~/projects/master-ai`, `~/projects/master-ai-cli`)
  without explicit instruction — it's unresolved, not accidental cruft, per
  the standing merge-conflict artifacts left in this repo's working tree.
