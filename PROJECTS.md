# Elijah's Projects

> Authoritative list of everything Elijah is building on your-machine.
> Ideas captured via `master.sh` menu option 9 auto-append to the Ideas / POCs section.
> **Project boards below are the dojo gate's source of truth.** Checkbox = source of truth for task state. Sensei reads this file on launch.

---

## Project Boards

### Master AI
- **Type:** master-bound
- **Role:** umbrella / meta — the brand and whole system
- **Gate:** always-available (Elijah is never locked out of his own frame)
- **Model:** auto   ← meta work = let the orchestrator pick per-question
- **Last:** 2026-04-18 — dojo gate feature wired into menu option 4 (in testing; not tagged v1.8 yet). Pupil HTML got Projects ▾ dropdown. Memory updated with 24/7 always-on design + pupil.html existence.
- **Goal:** shape the local-first offline AI brand into a sale-ready product; orchestrate every sub-UI
- **Tasks:**
  - [x] finalize Sensei dojo gate (this feature — currently in testing) — LOGIC-VERIFIED 2026-04-19 via `~/scripts/dojo_gate_test.sh` (14 PASS, 0 FAIL). CREATOR BYPASS: ~/.master_ai_creator in $HOME softens sealed mode to "skip allowed" (Elijah never takes the test — he built it). WELCOME-BACK MODE: first successful entry writes ~/.dojo_entered; returning users see their pinned project + task above a 🥋 "once you get a black belt, you're a black belt forever" banner, can continue / pick new / skip. pack_for_sale.sh rm's both markers so buyers face the first-time ritual.
  - [x] ship multi-user node support (up to 4 local users per machine) — plumbing complete 2026-04-19: master_ai.py profile-aware paths (✓), Pupil /profile namespace (✓), stt_server.py /sessions + /keys profile-aware (✓), master.sh banner shows active profile (✓), menu 15 Add User + menu 17 Switch User wired (✓). LIVE-TESTED 2026-04-19 via `~/scripts/multiuser_test.sh` — 18 PASS, 0 FAIL: two throwaway profiles, memory/chat/task isolation confirmed both directions, stt_server live-switches /profile without restart, default state untouched.
  - [x] wire Master AI Mesh (node-to-node federation) — scaffolding + federated routing both live 2026-04-19. Config: `~/.master_ai_mesh.json` (auto-seeds `mesh_token` on first run). Helper: `~/scripts/mesh.sh` (ls / add / remove / ping / ask / menu). Endpoints: `/node_info`, `/peers`, `/ask` (fail-closed on missing/bad token; 401 on bad, 503 when no token configured). Sensei: `mesh ls | ping | add | ask <peer> <prompt>`. master.sh menu 18. REMAINING (not blocking ship): discovery (mDNS / Tailscale enumeration), streaming replies for `/ask`, peer-selectable model routing.
  - [ ] multi-user friendliness + node-to-node AI sharing + resource split (free up RAM / split load across peer nodes so one user doesn't starve the others)
  - [x] retire master_ai.html (Pupil replaces it — 2026-04-19 decision; keep stt_server.py for /keys + /sessions) — DONE 2026-04-19: file moved to `~/scripts/archive/master_ai.html.deprecated`; all entry points (master.sh x3, inject_memory.sh, save_context.sh, howwework.txt x2, master_ai.py tailscale link) now point at pupil.html
  - [ ] chunker / `chunked:` Sensei command — **ARCHIVED 2026-04-19** (home problem: UX wasn't settled, kept being surfaced at wrong level; scripts stay on disk for un-archive later)
  - [ ] build $100 A+ version: agentic local coding (Claude Code parity), real curriculum (10+ classes per track), installer, multi-user, remote mesh
  - [ ] lock the "pack it up for sale" ritual (version tag + commit + memory update)
  - [x] fix stdin race in confirm prompts — Q2 typed while Q1's RUN/CREATE/EDIT/RUNTERM confirm is open gets swallowed as the 1/2/3/4 keystroke instead of queuing. Patch: two-channel stdin in master_ai.py (`_CONFIRM_IQ` + `_AWAITING_CONFIRM` flag; wrap the four confirm_ funcs; route in `_on_submit` + `_tui_input`). ~20 lines, no sensei_tui.py changes. **Do NOT re-enable the v1.7.11-reverted worker queue** — fix is stdin routing only. Confirmed by Elijah 2026-04-21 remote. Verified on disk 2026-04-27.
  - [x] investigate routing falling off master-ai — FIXED 2026-04-21 afternoon. Root cause: orchestrate() line 992-994 routed all prompts ≤20 words to qwen2.5:3b (the spark). Short ≠ simple — "fix the bug" is 3 words but needs senior-engineer reasoning. Branch deleted; short prompts now fall through to master-ai default. 3B reserved for idle tips + vision preprocessing only. Lives behind `refresh`.
  - [x] tmux mouse-mode toggle — DONE 2026-04-25: Sensei now has `mouse remote`, `mouse local`, and `mouse status`. `mouse remote` saves `SENSEI_MOUSE=1` and flips tmux mouse on for phone/RustDesk scrolling + taps. `mouse local` saves `SENSEI_MOUSE=0` and flips tmux mouse off so terminal drag-select copy works cleanly at your-machine. `launch_master_ai.sh` now reads `~/.master_ai_settings` instead of forcing `SENSEI_MOUSE=1` every launch. Use `refresh` after switching so the TUI relaunches with the saved app-level mouse setting.
  - [x] one-command health/productivity check — DONE 2026-04-25: Sensei now has `doctor` / `health`. It prints Pupil/Web UI, master-ai-ui.service, Ollama, required models, `/thoughts`, TTS, phone URL, mode/model/cloud keys, mouse profile, memory/approved counts, open tasks, pinned project/task, and latest crash line. This is the first stop after "doesn't work" and before long work sessions.
  - [ ] **Cruncher — hardware-aware data prep pipeline (NEW concept, NOT the archived chunker)** — Elijah 2026-04-21: *"takes a lot of data and breaks it down into the data that we can feed into our system specifically based off of our settings and what's hardware hard drive AI capabilities all of that, and once we throw our chunk in it, crunches it up and digested it into edible sections, and then it puts a timer up there to say based off this we will be able to feed this to sensei basically in this amount of time and it doesn't automatically pass it out so it runs smoothly."* Different from the archived `chunker.sh` (which was just text splitting). This is a **prep pipeline** that: (1) measures input size in tokens, (2) reads `selfscan.sh` output for hardware tier (GREEN/YELLOW/ORANGE/RED), (3) computes estimated wall-clock time = `tokens / measured_tok_per_sec` from the box's recent harvest data, (4) shows time estimate + chunk plan in a confirm prompt, (5) waits for user `go` before feeding chunks one at a time with pause between to let local model digest each piece. Surfaces in Sensei as `crunch: <paste>` or via menu. Lives at ~/scripts/cruncher.py. Reuses `master_ai_voice.json` thinking phrases between chunks. **Don't build tonight — too late in the session for clean implementation. Design first, build fresh.**

### Sensei
- **Type:** master-bound
- **Role:** execution dojo — where code gets made
- **Gate:** hard-gated, you must turn in a project with tasks to enter
- **Model:** qwen2.5-coder:7b   ← code-heavy execution
- **Last:** 2026-04-18 — `dojo_gate.sh` built + wired into `master.sh` option 4. `master_ai.py` now loads ACTIVE_PROJECT + ACTIVE_TASK from gate, shows PROJ/TASK in status bar, supports `dojo` / `dojo tasks` / `done` commands. Drift reminder (every 3000 chars) names the pinned task directly. Gate is soft in testing; hard once `~/.dojo_gate_sealed` exists. **Next pickup:** test flow end-to-end via RustDesk.
- **Goal:** stay the focused master-level execution layer; no distractions, pinned task, drift reminders
- **Tasks:**
  - [x] implement dojo_gate.sh (project picker + task generator) — verified on disk 2026-04-27.
  - [x] read active project/task from ~/.master_ai_active_project on startup — verified on disk 2026-04-27.
  - [x] show current task in status bar (SENSEI │ PROJ:... │ TASK:...) — verified on disk 2026-04-27.
  - [x] "done" command flips [ ] → [x] in PROJECTS.md and pulls next task — verified on disk 2026-04-27.
  - [x] tie 3000-char drift reminder to the pinned task — verified on disk 2026-04-27.
  - [ ] **smaller streaming-output viewer box inside the chat frame** — Elijah 2026-04-21: *"I'm thinking like internet TV — full size 1080p is gonna be rough, but 4:3 720p runs smooth and looks good."* Currently model output streams full-width into the chat frame, which on slow CPU + remote iPhone feels like a flood when chunks arrive. Proposal: render the streaming reply into a SMALLER child box (e.g. ~60% width, fixed height ~15 lines, auto-scroll) that sits inside the chat frame. When the stream completes, the content can stay as a bounded block or expand. sensei_tui.py (prompt_toolkit) layer — new Window with its own scroll behavior nested under the Frame. Visual trade: smaller viewport = smoother stream perception (human eye tracks less area), same info density.

### Pupil
- **Type:** master-bound
- **Role:** apprentice / workshop / intake UI
- **Gate:** open (this is where work comes IN — no gate to enter)
- **Model:** qwen2.5:7b   ← friendly 7B for teaching, brainstorming, scoping
- **Artifact:** `~/scripts/pupil.html` (1200 lines, martial-arts belt themes white/yellow/blue/green/purple/brown/black/hacker). Open: `file:///home/user/scripts/pupil.html`
- **Last:** 2026-05-12 — M0 Chrome extension bridge landed. `/chat` now runs through non-interactive `api_handle()` around `master_ai.handle()` and returns proposed typed actions; `~/scripts/sensei_extension/` has the MV3 side panel/content-script/options scaffold. **Next pickup:** load the extension unpacked, set `~/.master_ai_extension_token` in options, and test approving/rejecting `BROWSER_*` actions from a live tab.
- **Goal:** general-audience AI UI for users leveling up (beginner→intermediate→pro); students of the AI arts. Feeds polished artifacts up to Sensei who either masters them or humbles them back.
- **Tasks:**
  - [x] scaffold Pupil Chrome extension bridge — DONE 2026-05-12: MV3 side panel, service worker, content script, options page, token-aware `/chat` + `/stt`, and `/extension/action_result` audit loop live in `~/scripts/sensei_extension/`.
  - [ ] build greenfield intake ("I want to build X in bash/python")
  - [ ] build brownfield intake (existing code → graduate to production / App Store)
  - [ ] fold in Sensei Companion feature (phone-side check-ins, remote nudges)
  - [ ] fold in Sensei Notetaker feature (auto-log chats / decisions / ideas into project notes)
  - [ ] "hand up to Sensei" command — packages artifact, Sensei accepts or rejects

### Sunkissed Soul
- **Type:** master-bound
- **Role:** flagship experience — the front door of everything
- **Gate:** gated
- **Model:** auto   ← mixed work (code + vision for scanner + general); let orchestrator route
- **Last:** 2026-04-18 session 5 — `:5173` SKS Hub Client hardcoded `llama3` bug fixed (now defaults to `master-ai:latest`). Flagship arc expanded in memory: Sunkissed ties into Master AI + scrap scanner + apothecary + off-grid corpus. **Next pickup:** plug Sunkissed AI backend into Master AI orchestrator.
- **Goal:** the soul-companion UI; the AI that travels with the user. Currently paid on base44.com; local stack being built to replace it and house the full off-grid vision (scrap scanner + apothecary + knowledge spine).
- **Tasks:**
  - [ ] keep SKS Hub Client (:5173) pointing at master-ai:latest (done — don't regress)
  - [ ] plug Sunkissed AI backend into Master AI orchestrator (app-aware umbrella)
  - [ ] add scrap scanner UI hook (camera → material ID → blueprint)
  - [ ] add apothecary scanner UI hook (camera → plant ID → remedy)
  - [ ] design migration path off base44 (self-host the flagship)

### Off-Grid Civilization Information
- **Type:** master-bound
- **Role:** specialist model support — Master AI's side is the orchestrator routing + vision scanners. The kit / physical bundle / Scrappy fine-tune dataset live in a SEPARATE PROJECT at `~/off_grid_kit/`.
- **Gate:** gated
- **Model:** llava (vision, required for scanners) + routes to Scrappy when the specialist model is present on the box
- **Last:** 2026-04-19 — kit split into its own project folder. Master AI only keeps the orchestrator hook that routes survival-keyword questions to any `scrappy`-named model if one is pulled.
- **Goal (within Master AI):** wire the routing hook + scanner UIs. Fine-tune, dataset curation, and physical-kit work is not tracked here.
- **Tasks:**
  - [x] Orchestrator stub — auto-activates when any `scrappy`-named model is pulled; routes survival/off-grid keywords to it (master_ai.py `_scrappy_model_present()` + route #6b)
  - [ ] **scrap scanner v1** — phone/webcam photo → llava identifies objects → classify material → "worth keeping: Y/N, estimated use: …"
  - [ ] scrap scanner v2 — identified materials → specialist model returns buildable projects (requires Scrappy model pulled from the separate off-grid project)
  - [ ] apothecary scanner — photo of plant → llava IDs species → specialist model returns remedies

### Python & AI Apprenticeship
- **Type:** training   ← stays in Pupil forever; never graduates to Sensei
- **Role:** curriculum — levels you up to talk to Sensei properly
- **Gate:** N/A — lives in Pupil / menu option 13 (`learn.sh`)
- **Model:** qwen2.5:7b   ← patient 7B explainer; reliable, fits in RAM without swapping
- **Last:** 2026-04-18 — Dojo Bash Tutor added as menu item `b` in `learn.sh` (5-move copy-paste lesson teaching the moves behind the Sensei gate). Also surfaced in Pupil's Projects ▾ dropdown. **Next pickup:** check `~/.master_ai_progress.json` to see current unlocked/completed lesson; resume from `learn.sh` main menu.
- **Goal:** progressively unlock Python + AI skills through real builds; Pupil tracks what's tested, what's mastered, and proposes the next-level challenge. Training projects exist to *prepare* Elijah for master-level work, not to be shipped.
- **Progress source:** `~/.master_ai_progress.json` (unlocked lesson #, completed list, streak)
- **Adaptive loop:** when a task here is marked done → Pupil offers *"prepare a test for the next level?"* — not available on master-bound projects
- **Tasks:**
  - [ ] finish lesson 1 (terminal basics) via `learn.sh`
  - [ ] finish lesson 2 (Python syntax) via `learn.sh`
  - [ ] finish lesson 3 (file I/O + paths) via `learn.sh`
  - [ ] finish lesson 4 (calling Ollama from Python)
  - [ ] finish lesson 5 (build a tiny CLI that uses Ollama)

---

## Ecosystem notes

### Master AI (brand / umbrella)
- **Menu** — `~/scripts/master.sh` — top-level launcher, front door of the whole system
- **Sensei** — `~/scripts/master_ai.py` — tmux terminal AI agent (menu option 4)
- **Pupil Web UI** — `~/scripts/pupil.html` on `:8080/pupil.html` (served by `stt_server.py`). Replaced `master_ai.html` (archived 2026-04-19 at `~/scripts/archive/master_ai.html.deprecated`).
- **TTS Server** — `~/scripts/tts_server.py` on `:5050` (Piper voices, backend)

### Live Apps (external)
- **Sunkissed Soul** — base44.com — flagship app; local stack is being built to eventually replace the paid hosting

### Local App Builds
- **SKS Hub Client (local)** — `~/Downloads/sunkissed-soul` on `:5173` (Vite) — test harness for wiring the local Ollama hub into the real base44 Sunkissed Soul
- **SKS Hub Server** — `~/sks_hub.py` — Flask LAN knowledge library (built, not running)
- **Team Assist** — `~/test-interface.py` — Ollama model routing + chat stats (built, not running)
- **ai-master.sh** — `~/master-ai/ai-master.sh` — lightweight bash Ollama wrapper

### Infrastructure
- **Ollama** — `:11434` — local LLM runtime (master-ai:latest, qwen2.5:14b, qwen2.5-coder:7b, llava, qwen3.5:cloud, kimi-k2.5:cloud)
  - system-wide `OLLAMA_KEEP_ALIVE=30m` via `/etc/systemd/system/ollama.service.d/keep-alive.conf`
  - pre-warm on login: `master-ai-prewarm.service` (user) — loads master-ai before first query
- **RustDesk** — remote access (ID 1808427068)

### Sensei config (behavior & routing)
- **Orchestrator** — `orchestrate()` in `~/scripts/master_ai.py` routes to the right model/path (local / cloud-deep / cloud-fast / vision / ask-user / recall-memory / save-refresh)
- **Behavior contract** — `~/.sensei_behavior.md` — loaded into system prompt; defines the one-at-a-time workflow, ask-before-guess rule, visible [thinking] markers
- **Idle thoughts** — 💭 tips appear after 15s idle, rotate every 5s
- **Session auto-resume** — on context pressure (60k chars), Sensei auto-snapshots + soft re-execs + reloads full history on restart

---

## Ideas / POCs
<!-- auto-appended by master.sh option 9 -->

### Master AI — Multi-User Node (captured 2026-04-18 brainstorm)
- **Pitch:** each Master AI instance supports up to 4 local users, each with their own accounts / memory / sessions, sharing the one Ollama runtime
- **Why 4:** keeps CPU/RAM per-user reasonable on your-machine-class hardware; small group feel
- **Scope items:** local account setup (add user / switch user), per-user memory file, per-user session dir, Ollama check on startup
- **Touches:** master.sh menu (new "Users" section), master_ai.py (per-user paths via $USER or a sensei_user flag)
- **Status:** concept — next up after Sensei UI stabilizes

### Master AI Mesh — Node-to-Node Federation (captured 2026-04-18 brainstorm)
- **Pitch:** once a node has its 4 users, it can connect to other Master AI nodes (another 4 users each). A question asked on node A can route to a model/user on node B. Decentralized peer-network of personal AI instances
- **Why this matters:** breaks the single-user / single-machine ceiling without going to corporate cloud
- **Open questions:** discovery (LAN mDNS? Tailscale IPs? shared pubkey?), permissions (who can ask whose node?), routing rules (prefer local, fail over to peer)
- **Status:** vision — depends on multi-user node first

### Idea Manager — POC Merge/Delete/Track (captured 2026-04-18 brainstorm)
- **Pitch:** the `PROJECTS.md` Ideas / POCs section becomes a real little manager. Elijah can tell Sensei "merge ideas 1 and 3", "delete idea 2", "mark idea 5 as in-progress" — Sensei edits the file and keeps everything on track
- **Why:** vague ideas pile up; humans merge and prune. AI should understand the current state of the pile so suggestions stay coherent
- **Commands to wire:** `idea list`, `idea merge A B`, `idea delete N`, `idea status N <phase>`, `idea rename N "..."`
- **Status:** concept — small, high-leverage, probably first to actually implement

### AppForge — "app that makes apps" (captured 2026-04-18 brainstorm)
- **Pitch:** non-technical person describes what they want → wizard generates working code + sale-ready package
- **Elijah's core pains to solve:** (a) describing the idea clearly enough to get accurate code, (b) "what now?" after code exists (distribution, selling)
- **Output bundle user gets:** zip of runnable code + Gumroad listing scaffold + Android/iOS store submission checklist + sole-prop paperwork page
- **Stack vision:** start simple on phone (caches locally), syncs to desktop when back. Offline-first, uses local Ollama so it costs $0 to run
- **Evolution:** v1 = template-driven code gen, v2 = LLM-generated code, v3 = phone web UI + cloud sync
- **Scaffold on disk:** `~/scripts/appforge/` — forge.py wizard, Flask starter template (BUILD NOT STARTED — waiting for go-ahead)
- **Status:** POC / brainstorm phase
- **Philosophy note:** "this is just cosmetic — the real win is already having free offline AI that lets one person build a civilization in a terminal"

### POC (auto-logged 2026-04-21 17:30)
- **Ask:** can you make apps files, plans documents with plans of action?
- **Status:** brainstorming — scope-check fired

### POC (auto-logged 2026-04-23 08:08)
- **Ask:** could you code an app for me today that’s functional and ready to run
- **Status:** brainstorming — scope-check fired

### POC (auto-logged 2026-04-23 08:46)
- **Ask:** let’s discuss project plastic recycling press
- **Status:** brainstorming — scope-check fired

### POC (auto-logged 2026-04-24 20:11)
- **Ask:** create app
- **Status:** brainstorming — scope-check fired

### POC (auto-logged 2026-04-25 11:42)
- **Ask:** so how does apps like Kodak Claude code get produced and sold?
- **Status:** brainstorming — scope-check fired

### POC (auto-logged 2026-04-26 17:43)
- **Ask:** approvals is not Claude Code’s approval system. which approvals && approvals
- **Status:** brainstorming — scope-check fired

### POC (auto-logged 2026-04-27 20:30)
- **Ask:** we want to make apps
- **Status:** brainstorming — scope-check fired

### POC (auto-logged 2026-05-02 09:00 PM)
- **Ask:** whats next after i create the ultimate app
- **Status:** brainstorming — scope-check fired
