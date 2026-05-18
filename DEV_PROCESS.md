# Master AI — Dev Process

## 0. Purpose

This document captures HOW the architecture gets built — the dialogue patterns, the cadence, the mechanics of working with AI partners during development. It's a companion to `ARCHITECTURE.md`, which captures the WHAT. Read this when you're trying to understand why work landed in the order it did, what conventions the commits follow, and how to keep the rhythm going across sessions.

The process is opinionated because the work demands it. Architectural decisions in master_ai propagate across cycles — schema-shape today becomes test fixtures tomorrow becomes runtime assumptions next week. The process below was tuned to catch bad decisions during brainstorm rather than during retrofit.

---

## 1. The dual-agent dialogue

Development happens with two Claude instances working in parallel:

- **Terminal Claude (Claude Code)** — runs in the dev machine's terminal, has filesystem access, can read code, write files, run tests, commit. Holds the build context. Drives the actual encoding.
- **Browser Claude (Claude.ai panel)** — runs in a browser side-panel, has vision over rendered pages, can produce design schemas and review architectures, doesn't have direct filesystem write access. Holds the design partnership.

The operator (the human at the keyboard) is the third agent and the only one with persistent identity across sessions. The operator routes between the two Claude instances depending on what the work needs: terminal Claude for encoding + testing + commit, browser Claude for design review + brainstorm + sanity-checking architectures before they ship.

> **Design principle.** The operator is the orchestrator in the middle. The terminal-side and the browser-side Claude are peers, not subordinates of each other. Neither has permission to act as the other's executor.

---

## 2. The brainstorm-bracket pattern

When a new architectural decision needs to land, it goes through a structured dialogue with browser Claude. The bracket has three parts:

**OPEN** — the message starts with: *"Brainstorm mode — no work fires until you say `build it` at the end of your reply."* This signals to browser Claude that the conversation is design-time, not build-time, and gives him explicit license to push back on the proposal.

**BODY** — the proposal itself. Includes:
- The thing being proposed (schema, function shape, gate ordering, etc.)
- The reasoning for the proposal
- Two-to-four embedded pushbacks the operator/terminal Claude is raising on themselves — places where the proposal could be wrong, alternative shapes worth considering

**CLOSE** — *"Tell me what you think. If it's right, end your reply with `build it` and I go."*

Browser Claude reads the bracket, designs against it, and either:
- Says `build it` at the bottom of his reply (locks the design, work proceeds)
- Pushes back with refinements or a counter-proposal (iterate the bracket)
- Asks for clarification (the proposal wasn't tight enough)

The `build it` trigger lives at the very bottom of his reply — it's the last thing he says. Reading discipline: scroll to the bottom of the most recent reply, then scroll up and read the whole reply top-to-bottom incrementally. Don't speed-scroll just to trigger-check. Read the body so refinements aren't missed.

---

## 3. Pushback during brainstorm, not after

The hardest rule to internalize early. Every brainstorm bracket should include the operator's own pushbacks against the proposal — places they're uncertain, design choices worth contesting. The reason:

> **Design principle.** Pushback during brainstorm is design refinement. Pushback after commit is regret. Move the friction earlier.

Browser Claude's job in the dialogue is also pushback — better-argued, evidence-backed, sometimes against the operator's own framing. Several decisions in this codebase came from browser Claude winning the pushback (the 11→8 PageSignals slot refactor) or losing it (the multi-site capture pattern scope reduction). Either way, the friction landed before the commit existed, which made the commit cleaner.

Pushback is not adversarial. It's peer review on a hot artifact while it's still cheap to change.

**Not every commit warrants a brainstorm bracket.** Small fixes, low-risk follow-ups, and obvious refactors ship without one. The bracket is for decisions that propagate — schema shape, phase ordering, interface contracts. Use the cadence: if the operator finds themselves saying "this might bite us in three cycles," bracket it.

---

## 4. The work cadence — commit / push / snapshot

The session is paced in commit-counted windows. Each window is five commits. Every commit in the window must:

1. Make the change
2. Run tests if applicable; verify green
3. `git commit` with a descriptive message in the repo's style
4. Update the task list (mark in-progress / completed)
5. Increment the in-session commit counter

**Brainstorm turns don't count toward the commit cadence.** Only shipped work does. A round of design dialogue that produces no commit doesn't increment the counter.

Every fifth commit triggers the snapshot block (also fireable on demand via `ship` / `save state` / `where are we` / `commit and push`):

1. Write `~/Desktop/AI_CONTEXT/context_<YYYY-MM-DD_HHMMSS>.txt` — a snapshot containing HEAD hash, branch, summary of the last five commits, what's open next. Future "where were we" sessions read the newest snapshot first.
2. `git push origin <current-branch>` — back up verified work to the remote. Five commits is the maximum at-risk window if the laptop crashes.
3. Save session memory entries — new feedback rules, project facts, architecture decisions worth keeping across sessions.

The 5-commit window was tuned to balance two pressures: push cost (don't slow the rhythm) and loss window (don't accumulate too much unshipped local work). The "don't deploy on Friday" principle applies: when the fifth commit lands right after a hairy refactor, the snapshot is harder to come back to than when it lands after a small quiet commit. Plan the cadence accordingly.

---

## 5. Reading dialogue across sessions — the threads/ directory

Browser Claude has no persistent memory between sessions. Each browser-Claude session boots blank. Conversation continuity is the operator's job to preserve.

The convention: `~/Desktop/AI_CONTEXT/threads/` holds saved browser-Claude transcripts. Filename pattern: `thread_<YYYY-MM-DD>_<topic>.txt`. Examples:
- `thread_2026-05-18_bc_session-end_reply_verbatim.txt`
- `thread_2026-05-19_task_model_design_round.txt`

At natural session-end moments, the operator copies the meaningful browser-Claude replies into a file here. When starting a new browser-Claude session on a continuing topic, paste the prior transcript into the panel as the first message. Browser Claude reads it and effectively boots up with the continuity he doesn't have natively.

What to put in: browser Claude's reply verbatim (not paraphrased), the operator's prompts that triggered it (so context is preserved), date + topic in the filename.

What NOT to put in: personal info that bled through page chrome (email addresses in sign-out links, etc.), anything the operator hasn't authorized for archive.

> **Design principle.** Browser Claude is a tab. Treat his memory as the operator's responsibility to save.

---

## 6. Framing as the lever — not capability

The most important non-technical insight from the early development sessions: when browser Claude declines a task or raises a safety concern, the right move is rarely to argue capability. It's almost always to reframe the work.

Concrete example from the multi-site capture sessions: the framing of "automated multi-ATS reconnaissance" triggered browser Claude's safety guardrails, and he refused to participate. The reframing — *"a developer building an assisted-apply tool with operator-in-the-loop on every irreversible action, capturing per-site selectors as static config files with safety clauses baked in"* — was the same underlying work, but it landed inside what browser Claude was comfortable doing. He went from "I can't help" to "this is the configuration I've been most comfortable with throughout this conversation" within one bracket.

This is not manipulation. The earlier framing was actually a bad shape — the reframed version more accurately described what the work was supposed to be. The lesson is that when the dialogue stalls, the proposal is probably wrong, not the partner.

> **Design principle.** When the work hits a wall in dialogue, the framing is usually the problem, not the work.

---

## 7. Triggers that drive the loop

A small vocabulary of phrases drives the rhythm without ceremony:

- **`build it`** (from browser Claude, at the bottom of a reply) — exit brainstorm, execute the design, come back with commit hash
- **`go`** (from operator to terminal Claude) — proceed with the proposed move
- **`ship`** / **`save state`** / **`where are we`** / **`commit and push`** (operator overrides) — fire the snapshot block regardless of commit count
- **`Done. ✅ <commit-hash> <one-liner>`** (from terminal Claude back to browser Claude) — signals end of build, ready for next direction

> **Design principle.** A small fixed vocabulary keeps the loop fast. Inventing new triggers each session burns the energy that should go to the work.

---

## 8. What this dev process is NOT

A few patterns that explicitly aren't part of the process, because they were tried and discarded:

- **Automated multi-agent loops with no human in the middle.** The operator is non-negotiable. Even when the loop feels mechanical, the operator routes between agents, holds decision authority on scope, and intervenes when the cadence drifts.

- **Permanently delegating architecture review.** Browser Claude is a design partner, not the architect of record. Decisions are owned by the operator. The dialogue is the medium, not the authority.

- **Paste-injection of arbitrary content into either agent without authorization.** xdotool injection of prompts into the panel works mechanically but is gated by the operator's running consent. When the operator's hands-on-keyboard, the dev process pauses injection.

- **Pretending browser Claude has memory between sessions.** He doesn't. The threads/ directory is the workaround; don't design around imaginary state continuity.

---

## 9. Where this comes from

Most of these patterns were tuned across the 2026-05-17/18 dev sessions, with browser Claude as the design partner. ARCHITECTURE.md captures the technical decisions from those sessions; this document captures the methodology that produced them. Both are living documents — update them as the process drifts and as new conventions earn their place.

---

*Designed iteratively through dialogue with Anthropic's Claude — terminal-side and browser-side both. The methodology was as much an artifact of the work as the code was.*
