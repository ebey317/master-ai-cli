# Master AI 🥷

### A local-first AI agent CLI built from scratch because I couldn't afford the subscription.

I started with Anthropic's paid tier. I used it, I liked it, and I couldn't afford it. So I said: **I need this for free forever.** I found open models, I found free providers, and I built my own agent system from scratch — copy-pasting from terminals and chat boxes, learning as I went, no tutorial, no bootcamp, no CS degree.

That's what this is. Not a wrapper around someone else's product. My own AI agent — terminal-based, voice-enabled, vision-capable, multi-provider, MCP-integrated — running on my own hardware, on my own terms.

**Free. Local-first. Mine.**

---

## What It Does

**Two surfaces, one brain:**

- **Sensei** — the terminal side. Reads files, writes files, runs commands, manages memory, executes tools. The CLI you're looking at.
- **Pupil** — the browser side. Same brain, same memory, same keys. A browser extension for when you want a visual surface.

### Capabilities

| Feature | What It Does |
|---|---|
| **Multi-provider routing** | Routes between local models (Ollama) and cloud providers (OpenRouter, Groq, Gemini, Anthropic, OpenAI) based on task needs |
| **Voice (TTS)** | Text-to-speech server — the AI talks back |
| **Voice (STT)** | Speech-to-text server — talk to the AI instead of typing |
| **Vision** | Image processing via LLaVA and image generation via the image engine |
| **MCP integration** | Model Context Protocol — the AI talks to external tools through a universal protocol, not raw API calls |
| **Browser automation** | Chrome extension with content scripts, CDP wiring, form filling, file uploads, screenshot parsing |
| **Memory system** | Persistent memory across sessions — the AI remembers your projects, preferences, and context |
| **Subagent system** | Spawn specialized subagents for code review, file finding, test running, context inspection |
| **Safety layer** | Approval queue for destructive actions, irreversible-action heuristics, privacy cloud guard, permission manager |
| **Skill runtime** | State machine for skill execution — agents can learn and run new skills |
| **Multi-user profiles** | Up to 4 users per machine, each with their own memory and config |
| **Systemd services** | Autostart, TTS, UI, deep-clean timers — runs as a system service |
| **Self-diagnostics** | Self-scan tells you what your hardware can run before you start |

---


## Quick Start

1. Clone the repo:
   ```bash
   git clone https://github.com/ebey317/master-ai-cli
   cd master-ai-cli
   ```

2. Install via the provided script (recommended for first-time users):
   ```bash
   bash install.sh
   ```
   This will:
   - Copy files to `~/scripts/`
   - Set up systemd services
   - Prompt for Ollama and model downloads
   - Add `~/.local/bin` to your PATH

   Alternatively, install via pip (for developers):
   ```bash
   pip install -e .
   ```

3. Add your API keys (interactive bash prompt — Groq and OpenRouter have free tiers):
   ```bash
   bash setup_keys.sh          # paste keys, stored in ~/.master_ai_keys (0600)
   bash setup_keys.sh --check  # validate stored keys against live APIs
   ```
   Routing auto-detects whatever you configured: local Ollama by default,
   Groq fast lane, OpenRouter deep lane, Gemini free tier, Fireworks fallback.
   No key and no Ollama = startup gate blocks with instructions.

4. Run the agent:
   ```bash
   master-ai
   ```

5. (Optional) Full setup wizard (providers, Ollama models, permissions):
   ```bash
   master-ai --setup
   ```
   You can use a temporary GitHub Models assistant to walk through setup:
   ```bash
   GITHUB_TOKEN=ghp_xxx master-ai --setup
   ```
   The token is only used during setup; once complete, GitHub AI disconnects.

6. (Optional) Remove Master AI:
   ```bash
   master-ai --uninstall
   ```
   Levels: pip package + keys, all user data, or total wipe including Ollama.

6. (Optional) Launch the TUI:
   ```bash
   sensei
   ```

---

## The Architecture — The Nightclub

I had to understand this in my own words before I could build it. Here's how I explain it:

> It's a nightclub.
>
> **Linux** is the building. He owns the place. His rules, his club.
>
> **Master AI** is the superintendent. Employee on Linux's payroll. Runs the building, hires the staff, manages operations.
>
> **MCP** is the promoter. The reason the rest works at all. The DJ speaks one language, the building speaks another. Without the promoter, the DJ can't talk to the staff. The promoter is the protocol that makes cross-language interop happen. He walks the room, knows everybody, translates intent into instructions.
>
> **The AI** is the DJ. Works the booth, picks the vibe, speaks his own language — but can't reach the floor alone. The promoter relays every instruction.
>
> **The tools** are the staff. Bouncers, bartenders, runners. They do the jobs the promoter relays from the DJ.
>
> **You** are the operator walking in. Not staff. The one who wanted the party.
>
> You don't stumble into an MCP setup by accident. You're graduating to the invite-only club. Once you know who Linux is, what the superintendent does, why the promoter is the whole reason any of this works, what language the DJ speaks, and which kids do which jobs — you're not at the public party anymore. You're inside.

---

## Key Components

```
master_ai.py              14,083 lines — The core agent loop (handle → process_reply → dispatch)
sensei_tui.py              1,149 lines — Terminal UI
stt_server.py             4,108 lines — Speech-to-text server
sensei_reasoning_loop.py     465 lines — Reasoning loop for browser turns
skill_runtime.py             476 lines — Skill state machine (stdlib-only)
approval_queue.py            403 lines — Safety: approval queue for destructive actions
hooks.py                     428 lines — Pre/post hooks for file operations
capabilities.py              227 lines — Capability detection
observability.py             214 lines — Monitoring and observability
loop_fsm.py                  260 lines — Server-authoritative loop termination FSM
router.py                    111 lines — Model routing (local vs cloud)
subagent_registry.py         149 lines — Subagent registration and dispatch
tts_server.py                 84 lines — Text-to-speech server
```

### Browser Extension (Sensei Extension)
```
sensei_extension/          Chrome extension with:
  - content_script.js      — Page interaction, CDP wiring, form filling, uploads
  - service_worker.js      — Extension lifecycle and state
  - side_panel.js          — Side panel UI
  - native_messaging/      — Native host bridge to the CLI
  - options.js             — Configuration UI
```

### Subagents
```
subagents/
  code_reviewer.py         — Code review subagent
  context_inspector.py    — Context inspection
  file_finder.py          — File discovery
  test_runner.py          — Test execution
  spend_reporter.py       — Usage/cost tracking
  workflow_describer.py   — Workflow documentation
  directive_simulator.py  — Directive simulation
```

### System Integration
```
systemd/
  master-ai-tts.service       — TTS as a system service
  master-ai-ui.service        — UI server
  master-ai-prewarm.service   — Model prewarming
  master-ai-deep-clean.service/timer — Automated cleanup
```

---

## By the Numbers

| Metric | Count |
|---|---|
| Python lines | ~43,000 |
| Shell lines | ~10,000 |
| JavaScript lines | ~7,900 |
| HTML lines | ~2,200 |
| Test files | 57 |
| Total files | 236 |

---

## Why I Built This

I'm a self-taught developer. HVAC installer by day, building AI systems by night. I started with Anthropic's paid Claude, and it was good — but I couldn't afford it. I looked at the subscription and thought: **I need this for free forever.**

So I found Moonshot. I found open models. I found free API providers. And I started building — not a chatbot, not a wrapper, but a real agent system that runs on my machine, uses my API keys, follows my rules, and works whether I'm online or not.

I didn't know the word "MCP" when I started. I just knew the DJ couldn't talk to the staff without a promoter. So I built the promoter. Then I found out the industry had a name for it: Model Context Protocol.

I didn't know "agentic architecture" was a term. I just knew I wanted an AI that didn't just talk, but actually operated the machinery. So I built the agent loop, the tool calling, the reasoning cycle, the safety layer.

Everything in this repo came from the same place: **I couldn't afford it, so I built it.**

---

## Origin Story

- **April 2026** — Made a GitHub account. Empty profile.
- **May 13, 2026** — Built the first version of Master AI. Copy-pasting from terminals and chat boxes. No framework, no template. Just the ninja and the dojo.
- **May 19, 2026** — Published CLAF (Closed-Loop Agent Framework) as the public router layer.
- **June 2026** — Added browser automation, subagents, systemd services, 57 test files.
- **Present** — Running on Hermes Agent (free, open-source). The promoter still works. The nightclub is open.

---

## The Belt Journey

Master AI uses a martial arts belt system for its curriculum — 12 belt-graded lessons for Linux and Python. White belt to black belt. Because learning isn't a checkbox. It's a journey.

🥋 White → Yellow → Green → Blue → Brown → Black

I'm at green belt. Still learning. Still building.

---

## License

MIT — because it should be free. Like everything else that matters.

---

## API/Service Documentation

### Core Components

The Master AI system consists of several key components:

- **master_ai.py**: The core agent loop that handles the main processing pipeline
- **sensei_tui.py**: Terminal UI for interacting with the agent
- **verifiers.py**: Verification framework for ensuring system stability
- **router.py**: Model routing logic for selecting between local and cloud providers
- **approval_queue.py**: Safety layer for approving destructive actions

### Service Interfaces

The system provides several service interfaces:

1. **Command Line Interface (CLI)**: Run `master-ai` or `sensei` to start the agent
2. **Headless / Delegation Mode**: Run a task without the TUI
3. **TTS Server**: Text-to-speech service for audio output
4. **STT Server**: Speech-to-text service for voice input
5. **Vision Interface**: Image processing via LLaVA and image generation
6. **MCP Integration**: Model Context Protocol for tool communication
7. **Browser Extension**: Chrome extension for visual interaction

### Headless Mode

Master AI can be invoked non-interactively from another agent or script, similar
to Claude Code CLI's print mode. This is useful for automation, CI, and
delegation from tools like Hermes or other AI systems.

```bash
# Run a one-shot task
master-ai --task "Review src/auth.py for security issues" --headless

# Read task from a file
master-ai --task-file /tmp/task.md --headless

# Limit the number of tool turns
master-ai --task "Add a docstring to helpers.py" --headless --max-turns 5

# Output structured JSON
master-ai --task "Summarize the README" --headless --json
```

In headless mode, the TUI, banner, setup wizard, and permissions wizard are
skipped. The task is sent to the configured model and any returned tool
directives (`READ:`, `CREATE:`, `EDIT:`, `RUN:`, `SUBAGENT:`) are executed in
a bounded loop. Destructive shell commands are blocked by default.

### Configuration

Configuration is managed through:
- `setup_keys.sh`: For setting up API keys (stored in `~/.master_ai_keys`)
- `install.sh`: For system setup and installation
- `pyproject.toml`: For Python package configuration

## Contribution Guidelines

We welcome contributions! Here's how to get started:

### Getting Started

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature-name`
3. Make your changes
4. Run the test suite: `pytest`
5. Run linters: `ruff check .` and `black --check .`
6. Run shellcheck: `shellcheck *.sh`
7. Commit your changes with descriptive messages
8. Push to your fork
9. Open a pull request

### Code Style

- Follow PEP 8 guidelines
- Use 88-character line length (Black)
- Use ruff for linting
- Write unit tests for new functionality
- Keep functions small and focused
- Document public APIs with docstrings

### Testing

- Write unit tests in the `tests/` directory
- Test file names should start with `test_`
- Use descriptive test names
- Cover edge cases and error conditions
- Run tests before submitting a PR

### Documentation

- Update README.md when adding new features
- Add docstrings to new functions and classes
- Keep documentation in sync with code changes

### Security

- Never commit API keys or secrets
- Use the approval queue for destructive actions
- Follow the safety layer guidelines in approval_queue.py
- Test for security vulnerabilities

### Review Process

- PRs will be reviewed by the maintainers
- Tests must pass before merging
- Code style must be consistent
- Documentation must be updated
- Changes should be backward compatible when possible

---

## Author

**Elijah Wilkins** — self-taught AI systems builder, HVAC installer, and creator of Master AI.

> *Working in the gray. Turning words into manifestation.*