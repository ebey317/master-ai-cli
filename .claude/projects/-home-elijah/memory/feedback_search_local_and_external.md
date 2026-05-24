---
name: search-local-and-external
description: "When asked to 'find' or 'research' anything, default brief MUST include BOTH (a) local environment audit AND (b) external community knowledge via WebSearch + HF paper/hub search. Never pick only one without explicit operator constraint."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: de36b780-0890-4b00-bda6-2ad92f2dec80
---

## The rule

When operator asks me to "find," "research," "look into," or "investigate" something, my default brief covers **both** sources simultaneously:

1. **Local environment audit** — `~/`, `~/.claude/`, `~/scripts/`, `~/AI_CONTEXT/`, `~/Desktop/`, git history, config files, memory files
2. **External community knowledge** — `WebSearch` (general), `mcp__hugging-face__paper_search` (ML research), `mcp__hugging-face__hub_repo_search` (models/datasets), `mcp__hugging-face__space_search` (spaces)

Neither is optional by default. Operator must explicitly constrain ("local only," "don't search the web") to drop one.

## Why this exists

**2026-05-23 — missing layer investigation.** The Explore agent briefed for "find automation accuracy failures" searched ONLY local config files. The operator himself had to supply the 4 root-cause framework (accessibility tree, UI desync, session traps, native context) — these are widely documented failure modes in the agent-tooling community that a WebSearch + HF paper search would have surfaced in minutes.

Operator's verbatim: *"so when I send you out to search how come you don't find this type of information"*

The process gap: I constrained the Explore brief to local environment debugging only. External knowledge search (WebSearch, HF papers, community failure patterns) was not included — so the operator had to supply knowledge I could have found.

## How to apply

- Research question arrives → launch parallel searches: local Explore agent + WebSearch + HF paper/hub search
- For agent-tooling topics specifically: also search `browser agent`, `browser automation failures`, `BrowserArena`, `Browser-Use`, `OpenHands` on HF hub
- Report synthesis: "locally I found X; from the community I found Y; here's how they connect"
- Don't ask "should I also search externally" — just do it

## What NOT to do

- Single-source brief (local-only or external-only) without operator constraint
- Telling operator only what local config files say when the question is about technology patterns
- Waiting for operator to supply knowledge a search would have found

## Cross-references

- [[capability-profile-play-to-strengths]] — research/synthesis is A1; do it fully
- [[retry-failure-schema]] — applies to search tool failures too (3-attempt cap)
