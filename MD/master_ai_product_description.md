# Master AI

**Local-first computer agent with optional cloud escalation.**

By Elijah W. Sr. — free-thinking garage scientist.

---

## What it is

Master AI is a personal computer agent that lives on your own machine. It
runs offline by default on a local language model, and only reaches for
the cloud when you tell it to. No SaaS subscription. No telemetry. No
Claude API. Bring your own keys for the optional cloud lanes.

Two surfaces, one brain:

- **Sensei** — the terminal agent. She reads files, runs commands, opens
  apps, edits code, manages memory. Stoplight modes: Plan (red, propose
  only), Review (amber, one step at a time), Auto (green, flow through
  with safety gates).
- **Pupil** — the browser UI. Same brain, different skin: chat in the
  side panel, in-page actions through a Chrome extension, voice in,
  pop-up out.

---

## Who it's for

People who think of the computer as something they program, not just
something they use. Operators, makers, off-grid builders, anyone who
wants a real assistant on their own metal — no cloud lock-in, no monthly
bill, no upload of their files to a third party.

---

## What it can do

### Local terminal lane
- **RUN** — execute any bash command with captured output (ls, git,
  pytest, apt, curl).
- **RUNTERM** — spawn an interactive command in a new graphical terminal
  (TTY-style or visual scripts).
- **READ** — read any file (text, JSON, PDFs via extraction).
- **CREATE** — write a new file with a full content block.
- **EDIT** — patch an existing file with find/replace markers.

### Browser lane (Chrome MV3 extension)
- **BROWSER_NAV** — open any URL.
- **BROWSER_CLICK / BROWSER_DOUBLE_CLICK** — click DOM elements.
- **BROWSER_FILL** — type into a form field.
- **BROWSER_FILL_FORM** — auto-fill a whole visible form from your saved
  profile.
- **BROWSER_SUBMIT** — submit forms through the page's real handler.
- **BROWSER_READ_PAGE** — read the page's accessibility tree, visible
  text, and selectors.
- **BROWSER_READ / BROWSER_EXTRACT_LIST** — pull structured rows from a
  list, grid, or specific region.
- **BROWSER_FIND** — locate visible text and get a selector.
- **BROWSER_SCROLL** — scroll up, down, top, bottom, or by viewport.
- **BROWSER_SCREENSHOT** — capture viewport or full-page PNG.
- **BROWSER_WAIT** — pause for page rendering.
- **BROWSER_JS** — execute a short JavaScript snippet and get its return.
- **BROWSER_CONSOLE / BROWSER_NETWORK** — read console logs or recent
  network activity.
- **BROWSER_CDP_MOUSE / BROWSER_CDP_KEY** — low-level keyboard and
  mouse control through Chrome DevTools Protocol when DOM selectors
  aren't enough.
- **BROWSER_RESIZE_WINDOW** — resize the browser window.
- **BROWSER_DRIVE_INSPECT_FOLDER** — inspect the current Google Drive
  view.
- **BROWSER_WORKSPACE_OPEN/READ/WRITE/CLEAR** — Sensei's in-browser
  writing pad.

### Reasoning and memory
- **reason: / reason fast/standard/deep/max** — multi-step planner +
  critic + finalizer chain for hard problems.
- **REMEMBER** — store a durable fact across sessions.
- Auto-memory at `~/.master_ai_memory` — survives reboots, persists
  context across days and weeks.
- SQLite FTS5 full-text search over her own audit log, harvest log,
  and memory stores — instant recall.
- Pure-analysis reflector — aggregates recent activity into a digest
  she can read on demand.

### Voice
- **STT (Whisper)** on port 5050 — speak to her.
- **TTS** — she speaks back.

### Vision
- **llava** locally for image understanding (no internet required).
- **Gemini 2.0 Flash** when you're connected and want faster vision.

### Honest knowledge of her own box
- Tool detector enumerates ~95 common CLIs on your machine at startup
  and writes a JSON inventory. Sensei queries it with `jq` before
  answering "do I have X installed" questions — never guesses.
- Categories scanned: vcs, editors, languages, network, files/text,
  media, databases, browsers, desktop control, system monitors,
  crypto, email clients, extras.

### Local image generation
- `image: <prompt>` runs Stable Diffusion locally on CPU
  (sd.cpp + LCM + TAESD). ~56 seconds per image. No cloud required.

### Job-application helpers
- Profile cache for filling applications.
- Application log.
- Deferred-submit gate — first application of a session pauses for
  your review before clicking "Submit."
- Fair-chance filtering, employer exclusions, salary and type
  constraints.

### Optional cloud escalation (BYOK)
- **Groq** — fastest cloud lane, OpenAI-shape API, 400+ tokens/sec.
- **DeepSeek R1 / OpenRouter** — deeper reasoning chain.
- **Gemini 2.0 Flash** — grounded Google search, vision.
- **Cerebras** — high-throughput inference.
- **Ollama Cloud** (qwen3.5, kimi) — extended local-family models.
- **Fireworks** — additional cloud routes.

Cloud is opt-in per request (`fast:` / `deep:`) or opt-in per session
(`mode connected`). The product works fully offline if every cloud is
dark.

---

## What it isn't

- Not a SaaS — runs on your own computer.
- Not a chatbot — it's an agent that takes real action: opens apps,
  edits files, drives the browser, manages your machine.
- Not telemetry-bound — nothing leaves the box unless you point it at
  a cloud lane.
- Not built on Claude API — Master AI is independent of Anthropic's
  paid API.
- Not a black box — every directive is auditable, every action gates
  on mode, every cloud call is logged.

---

## How to run it

- Requires Ollama with `qwen2.5:7b` pulled (the brain) and `llava`
  (for vision).
- Sensei terminal: type `sensei` after install.
- Pupil browser UI: type `master` for the main menu, pick option 5.
- Linux today (Ubuntu / Linux Mint tested). Android client in the
  roadmap.
- Free, local-first, BYOK for cloud.

---

## Contact

Elijah W. Sr.  
ebey317@gmail.com
