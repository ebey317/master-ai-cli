# Sensei Brainstorm Mode — Merge-to-Consensus Planning

**Author:** Elijah (ebey317)
**Status:** Concept proven 2026-09-06 (live test, OpenRouter free tier)
**Location:** `sensei_reasoning_loop.py` (to be wired as `plan_debate` mode)

---

## The Idea

Take two models, have them brainstorm a plan **together**, and force them to
converge on **one** plan instead of arguing forever.

The problem everyone else hit: multi-agent debate loops never terminate —
each model keeps finding new flaws in the other's plan, so they critique
forever and never agree.

The fix is three moves nobody combined before:

1. **Merge** — collapse the two parallel plans into ONE unified plan.
2. **Keep what we agree on** — only fix the disagreements, never add scope.
3. **Verdict gate** — every revision ends with `critique it` or `build it`,
   emitted FIRST so truncation can't eat it.

---

## The Mechanism

```
1. SIMULTANEOUS SEED
   Model A and Model B both get the task at the same time.
   Each writes its own plan.  (two parallel plans)

2. MERGE
   One model receives BOTH plans and produces ONE unified plan.
   From here there is only ONE plan — a single target to converge on.

3. CRITIQUE / REVISE LOOP  (only AFTER the merge)
   Rule: "Keep everything the two plans already agree on. Only change
   the parts where they disagree or where a real gap exists. Do not add
   new scope."

   a. Critic flags only the real flaws/gaps/disagreements.
   b. Reviser fixes ONLY those points, keeps the rest.
   c. Reviser emits a verdict on the FIRST line:
        "critique it"  → another round
        "build it"     → hold for the other model's verdict

4. CONVERGENCE
   When the verdict is "build it", the loop stops.
   That converged plan is the ONE plan handed to the orchestrator.
```

---

## The Five Fixes That Made It Work

These are the load-bearing details. Each one was discovered by a live
failure during testing.

| # | Fix | Why it matters |
|---|-----|----------------|
| 1 | **Merge step** | Two parallel plans diverge forever. Collapsing to one plan gives the loop a single target. |
| 2 | **"Keep what we agree on"** | Without it, the reviser adds every library/idea mentioned, bloating the plan each round. |
| 3 | **Verdict-first** | Emit `build it`/`critique it` on the FIRST line, not the last. Truncation eats the tail, so a last-line verdict never lands. |
| 4 | **Non-reasoning model for the verdict** | Reasoning models (Nemotron Ultra) monologue instead of obeying a binary instruction. Instruction-following models (minimax-m3, laguna-s-2.1) emit a clean verdict. |
| 5 | **Bigger token budget** | The plan must actually fit, or it's always "incomplete" and never "build it". |

---

## Model Choice Matters More Than Prompt Design

The verdict step needs a model that obeys a binary instruction, not a
reasoning model that narrates its own process.

Tested (OpenRouter free tier, 2026-09-06):

| Model | Verdict behavior |
|-------|------------------|
| `minimax/minimax-m3:free` | ✅ Clean "build" / obeys |
| `poolside/laguna-s-2.1:free` | ✅ Clean "build" / obeys |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | ❌ Monologues, never commits |
| `nvidia/nemotron-3-super-120b-a12b:free` | ❌ Rambles 50-item critiques |
| `google/gemma-4-31b-it:free` | ⚠️ Rate-limited (429) |

---

## Live Test Result

Task: "Build a cron job that checks the weather every morning and texts me
if it will rain."

- Simultaneous seed: ✅ two solid plans
- Merge: ✅ one unified plan
- Critique/revise with keep-agree rule: ✅ no bloat
- **Converged in ONE round → "build it"**

Final plan was production-ready: 10 steps, rain-detection logic, secure
secrets, cron + timezone, error handling, dedupe state file, 7-day
monitoring. Self-estimated ~1–2 hours, $0/month.

---

## Prior Art (honest accounting)

The *ingredients* exist in research:
- Multi-agent debate (NeurIPS 2024, arxiv surveys)
- "Ensemble" primitive — vote/merge/filter (LLM multi-agent surveys)
- "Auto-Merge" sub-plan merge prompt (arxiv 2026)

The *recipe* — merge → keep-agree → verdict-gate that actually terminates —
is Elijah's own. That's the part everyone got stuck on.

---

## Next Steps

- [ ] Wire as `plan_debate` mode in `sensei_reasoning_loop.py`
- [ ] Add `--planner-a` / `--planner-b` / `--merger` CLI flags
- [ ] Add max-rounds safety cap (even good models can loop)
- [ ] Persist the debate transcript for audit
