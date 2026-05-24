# HF Hub Deep Dive — Retry / Failure / Error-Handling Schemas

Validation pass on the canonical schema (max_attempts=3, TRANSIENT/INTERMITTENT/PERMANENT/AUTH/OBSERVABILITY_FAILURE/SEMANTIC_NO_OP, circuit breaker, on_stop fallback chain) against what is actually on Hugging Face Hub.

## 5 Notable Sources

1. **[AgentErrorTaxonomy / AgentDebug — "Where LLM Agents Fail and How They can Learn From Failures"](https://hf.co/papers/2509.25370)**
   Splits failures across **memory / reflection / planning / action / system-level** axes — a vertical (where in the agent loop) cut where ours is horizontal (what kind of error). Their "system-level" bucket is the closest analog to our TRANSIENT/INTERMITTENT/AUTH and validates that grouping. Their **reflection** failure class is something we don't carve out — i.e. the agent retried but failed because its *self-critique* was wrong, not because the tool was wrong.

2. **[SHIELDA — Structured Handling of Exceptions in LLM-Driven Agentic Workflows](https://hf.co/papers/2508.07935)**
   Introduces a **reasoning-phase vs execution-phase** exception split with an explicit *exception classifier → handling pattern registry → structured executor → phase-aware recovery → cross-phase recovery*. Their pipeline shape mirrors ours, but they formalize "cross-phase recovery" (an execution error escalating to a reasoning-level replan) — that is our `on_stop_fallback` chain made first-class instead of a list.

3. **[Graph-Based Self-Healing Tool Routing for Cost-Efficient LLM Agents](https://hf.co/papers/2603.01548)**
   Replaces LLM-driven retry with a **cost-weighted tool graph + Dijkstra shortest-path** over parallel **health monitors**. Their **silent-failure** detection is a generalization of our `same_observable_state_after=2` and `OBSERVABILITY_FAILURE` class. Notable difference: routing chooses an alternate tool **deterministically before** the LLM is even re-invoked, cutting model spend.

4. **[ReliabilityBench](https://hf.co/papers/2601.06112)**
   Defines a **reliability surface R(k, ε, λ)** with `pass^k`, semantic perturbations, **action metamorphic relations**, and **chaos-engineering fault injection**. We have stop conditions but no metric for *how reliable* a tool actually is over k attempts — this gives us a way to *tune* `max_attempts` per tool from data rather than picking 3 globally.

5. **[smolagents docs — RETRY_MAX_ATTEMPTS + max_steps + step_callbacks](https://huggingface.co/docs/smolagents/reference/models)** and **[Building Good Agents](https://huggingface.co/docs/smolagents/tutorials/building_good_agents)**
   HF's own reference implementation. Two concrete patterns: (a) `retry: bool` is **scoped only to rate-limit errors**, not all transient — i.e. they distinguish "model API 429" from "tool returned garbage" and only auto-retry the first. (b) `step_callbacks` per `MemoryStep` type — retries are observed via callback rather than embedded in the retry loop, giving operators a hook to inject the `operator_eyes` fallback without rebuilding the loop. The simplification thesis in *Building Good Agents* ("reduce LLM calls, reduce error risk") is the meta-argument behind our `SEMANTIC_NO_OP` class.

Honourable mentions: **[Confidence Dichotomy / Tool-Use Miscalibration](https://hf.co/papers/2601.07264)** (calibration-aware retry — if model confidence is low, route to fallback before retry), **[AgenTRIM](https://hf.co/papers/2601.12449)** (status-aware validation = a permission-layer addition to our taxonomy), **[Diagnosing Failure Root Causes in Platform-Orchestrated Agentic Systems](https://hf.co/papers/2509.23735)** (counterfactual root-cause analysis on failure logs), **[BrowserArena](https://hf.co/papers/2510.02418)** (concrete browser failure modes: captcha, pop-ups, direct nav — operationalizes our OBSERVABILITY_FAILURE for browser cables).

## Anything missing from our current schema?

Three gaps the HF community has filled that we haven't:

- **Phase axis is missing.** We only classify *what* went wrong (TRANSIENT etc.). [SHIELDA](https://hf.co/papers/2508.07935) and [AgentErrorTaxonomy](https://hf.co/papers/2509.25370) both add a *where* axis — reasoning-phase vs execution-phase, or planning/action/reflection. A retry that re-runs the same plan against a flapping tool burns budget; a phase tag would let the policy choose between "retry the tool" vs "replan and retry." Recommend adding `phase: {planning, action, reflection, system}` alongside the existing `class`.
- **Reflection-failure class is missing.** Our schema assumes the tool is the failure surface; AgentErrorTaxonomy shows a real failure mode is the agent's own *self-critique being wrong* — retrying then loops on a false premise. This deserves a 7th class, `REFLECTION_FAILURE`, with `on_stop_fallback: replan_with_external_grounding` (i.e. don't trust the agent's own verdict — go to operator_eyes earlier).
- **No reliability metric → max_attempts is a guess.** [ReliabilityBench](https://hf.co/papers/2601.06112) and [Graph-Based Self-Healing](https://hf.co/papers/2603.01548) both per-tool / per-route the retry budget from measured `pass^k`. Hard-coding `max_attempts=3` for every tool wastes budget on flaky ones and gives up too early on reliable-but-slow ones. Recommend `max_attempts` become a *per-tool* value sourced from a rolling reliability metric, with `3` only as the cold-start default.

Minor additions worth considering: a **calibration gate** ([Confidence Dichotomy](https://hf.co/papers/2601.07264)) — when model self-confidence on the tool call is low, skip retry and jump to `alternate_tool`; and a **permission/status precheck** ([AgenTRIM](https://hf.co/papers/2601.12449)) to short-circuit AUTH failures before the first attempt is even spent.
