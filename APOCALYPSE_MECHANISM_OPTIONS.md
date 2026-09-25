# Apocalypse Mode — What Fills the Gap After the Chunker?

**Status: OPEN DECISION** — Elijah's call. This doc lays out four options, trade-offs, and a recommendation.

**Saved:** 2026-04-19

## Background in one paragraph

Master AI has two product moods: **Local Mode** (default — never needs the internet, everything runs on your machine) and **Connected Mode** (opt-in — prefers fast cloud brains when you have API keys). The internal codenames are "apocalypse" and "peacetime." For a while, the **chunker** was the thing that made Local Mode genuinely capable of hard work — it broke big questions into chunks a 7B brain could handle sequentially, then stitched them together. But the chunker was a "leaf falling off a tree" — no natural home in the product, so it got archived 2026-04-19. Local Mode still exists, still routes, still works — but the question is: **what's the thing that makes Local Mode genuinely strong, not just functional?**

## Option A — The Reasoning Loop IS the new mechanism

**The idea:** `think:` / `think fast:` / `think deep:` already forces a 7B brain through Planner → Solver → Critic → Finalizer. That's structured cognitive decomposition — the exact thing the chunker was doing, but for REASONING instead of CONTEXT LENGTH. The reasoning loop runs entirely local, needs no internet, handles the hard questions Local Mode would otherwise punt to cloud.

**What it opens up:** "your local AI thinks as carefully as Claude, just slower." The wedge is structured cognition, not parameter scale.

**What's already done:** the loop is built, wired, and safe to run (S01 cap applied). Docs exist. Elijah tested it and both brains completed before the freeze.

**What's still needed:** nothing in code. Needs marketing copy and one lesson in Pupil showing buyers when to use `think:`. Maybe a Pupil ▾ entry: "Deep Think (takes 2 min, answers like Claude)."

**Trade-offs:**
- Pro: it's real, tested, uses infrastructure already on disk
- Pro: honest framing — "4 stages of thinking" is a concrete, understandable differentiator
- Con: wall-clock is 60–300s per question. Slower than a cloud brain by a lot.
- Con: doesn't help with context-length problems (original chunker's domain). Hard questions that need LONG context still punt.

---

## Option B — Retrieval over the off-grid corpus

**The idea:** when cloud is gone, the apocalypse "trick" is that Master AI has a stored knowledge base on-box (the off-grid corpus — already in `~/off_grid_kit/`). Build a retrieval layer: every local question gets a RAG step first, relevant chunks get injected into the prompt, then the 7B brain answers with real grounded context.

**What it opens up:** "when the internet dies, your AI still has the answers — because the answers are literally on your USB stick." Strongest marketing hook for the apocalypse brand.

**What's already done:** `~/off_grid_kit/` exists as a separate project. No retrieval layer yet — no embedding index, no RAG pipeline.

**What's still needed:** embedding model (local — probably nomic-embed-text via Ollama), vector store (sqlite-vec or chroma — both local), corpus ingestion script, retrieval injection in `orchestrate()` ahead of the local brain call. 1–2 weeks of build, rough estimate.

**Trade-offs:**
- Pro: genuinely differentiating — nobody else ships a "turn off your wifi and still get answers from stored knowledge" product
- Pro: directly serves the off-grid / Sunkissed vision
- Con: real build effort. Embedding, indexing, retrieval UI all need to exist
- Con: corpus curation is its own ongoing job
- Con: "RAG over local corpus" is common — needs the SCALE and CURATION of the corpus to feel magical

---

## Option C — Specialized fine-tuned brains (Scrappy pattern)

**The idea:** the orchestrator already routes survival keywords to any `scrappy`-tagged model. Extend this — every hard domain gets its own fine-tuned small brain. Scrappy for survival, a code-specialist (already have qwen2.5-coder), maybe a gardening/medical/mechanics brain later. Apocalypse Mode means routing to domain specialists, not generic models.

**What it opens up:** "your AI knows THIS thing really well because it was trained on it." Narrative is simple and ownable.

**What's already done:** Scrappy hook is wired. Off-grid kit has a fine-tune pipeline sketched.

**What's still needed:** actually running the fine-tune (training data, compute, eval). Each specialist is its own project.

**Trade-offs:**
- Pro: lines up with the user→operator arc — buyers can "teach" their AI by pulling specialist models
- Pro: no architecture work needed — the hook already exists
- Con: fine-tuning is expensive compute. Each specialist is weeks of work.
- Con: it's a feature roadmap, not a single named mechanism

---

## Option D — Drop the "apocalypse mechanism" frame entirely

**The idea:** Local Mode is already the default. It routes, it runs, it works. There doesn't need to be a CAPITALIZED MECHANISM with a name. The chunker was a single-feature obsession. Instead, quietly deliver on "local-first" with the tools already in place (reasoning loop + orchestrator + local brains) and let the EXPERIENCE be the mechanism.

**What it opens up:** simpler positioning. No need to explain what "apocalypse mode" does beyond "runs offline."

**What's already done:** everything. The mode exists. No special mechanism is required to make it real.

**What's still needed:** rename "apocalypse" → "Local Mode" in all user-facing copy (code comments can keep the playful name). Remove the "apocalypse mechanism is OPEN" language from memory. Treat it as settled.

**Trade-offs:**
- Pro: zero engineering cost. Just a framing move.
- Pro: honest — Master AI IS already local-first
- Con: gives up the marketing hook. "Apocalypse Mode" is a memorable phrase
- Con: implies scope-reduction, which may feel like losing something

---

## Recommendation

**Option A + a half of Option D.**

The reasoning loop is already built and IS structured local cognition — that's the spiritual successor to the chunker. Call it what it is ("Deep Think" in user-facing copy, "reasoning loop" in code) and ship it as the Local Mode differentiator. Don't invent a separate MECHANISM name for apocalypse — the mode name is enough.

Keep Option B on the roadmap as the v2 move. When the off-grid corpus grows past N MB, the retrieval layer becomes the obvious next differentiator.

Keep Option C as an ongoing community pattern — Scrappy exists, other specialists CAN be pulled, no need to force it as the mechanism.

## What Elijah has to decide

1. Which option (or combination)?
2. If A: should `think:` get a Pupil ▾ entry and a lesson? (easy, in Claude's lane to write)
3. If B: is the off-grid corpus ready to build retrieval against, or does corpus curation come first?
4. Should the memory file `project_apocalypse_mode.md` be updated with the resolution, or stays "OPEN" until Elijah commits?

Nothing needs to happen today. This doc preserves the decision space.
