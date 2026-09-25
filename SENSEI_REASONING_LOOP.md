# Sensei Reasoning Loop — Design Spec

## Purpose

Improve reasoning quality of small-to-medium local LLMs (7B–14B: Qwen, Llama, etc. via Ollama) by forcing multi-pass structured cognition instead of single-pass inference. The model still fits in 5–10 GB of RAM; the **structure** does the work that parameter scale would otherwise do.

Named **Sensei Reasoning Loop** — the same AI model plays four different roles in sequence, each with a narrow task.

Distinct from the `loop:` feature inside Sensei (which executes shell steps with critique). The Reasoning Loop is pure text — for answering hard questions where a single-shot reply would drift.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                     SENSEI REASONING LOOP                      │
│                                                                │
│   user query                                                   │
│        │                                                       │
│        ▼                                                       │
│   ┌─────────────┐                                              │
│   │  PLANNER    │  decompose · assumptions · constraints       │
│   │  (no solve) │  returns: {assumptions, constraints, steps}  │
│   └─────────────┘                                              │
│        │                                                       │
│        ▼                                                       │
│   ┌─────────────┐                                              │
│   │   SOLVER    │  execute each step · show work               │
│   │             │  returns: {reasoning, raw_solution}          │
│   └─────────────┘                                              │
│        │                                                       │
│        ▼                                                       │
│   ┌─────────────┐                                              │
│   │   CRITIC    │  find errors · missing cases · weak logic    │
│   │   (skip in  │  returns: {issues, corrections}              │
│   │   fast mode)│                                              │
│   └─────────────┘                                              │
│        │                                                       │
│        ▼                                                       │
│   ┌─────────────┐                                              │
│   │  FINALIZER  │  clean answer · no reasoning artifacts       │
│   │             │  returns: {answer}                           │
│   └─────────────┘                                              │
│        │                                                       │
│        ▼                                                       │
│   final answer to user                                         │
└────────────────────────────────────────────────────────────────┘
```

---

## Data flow

Each stage emits a strict JSON object. The orchestrator feeds the previous stages' outputs as context into the next stage's prompt.

**Planner →**
```json
{
  "assumptions": ["..."],
  "constraints": ["..."],
  "steps": ["...", "..."]
}
```

**Solver →**
```json
{
  "reasoning": "step-by-step chain-of-thought, markdown allowed",
  "raw_solution": "the proposed answer before review"
}
```

**Critic →**
```json
{
  "issues": ["..."],
  "corrections": ["..."]
}
```

**Finalizer →**
```json
{
  "answer": "clean user-facing response, no scaffolding"
}
```

If a stage's model returns non-JSON, the orchestrator falls back to the raw text so the pipeline still progresses — degradation is graceful.

---

## Modes

| Mode | Stages run | Use when |
|---|---|---|
| `fast` | planner → solver → finalizer | speed matters; critic skipped |
| `standard` (default) | planner → solver → critic → finalizer | balanced; good for most hard questions |
| `deep` | planner → solver → critic → refine-solver → critic → finalizer | final accuracy; two critique passes |

---

## Per-stage model config

Each stage can use a different Ollama model. Defaults (set for Qwen 2.5 stack):

| Stage | Default model | Why |
|---|---|---|
| planner | `qwen2.5:7b` | decomposition needs coherence, not size |
| solver | `qwen2.5:7b` | code/math reasoning; 14b if pulled |
| critic | `qwen2.5:7b` | catches solver's blind spots with a different role prompt |
| finalizer | `qwen2.5:3b` | trimming prose is cheap — spark is plenty |

All four can be the same model. Swapping in a larger model only at the critic stage often beats running the whole pipeline on the larger model.

---

## Memory persistence (optional)

If a memory file path is provided, the orchestrator:
1. Reads prior reasoning loops from `memory.jsonl` (one JSON object per line).
2. Prepends the 3 most recent (query, answer) pairs to the planner's context.
3. Appends the new loop's full transcript after finalize.

This lets the pipeline build up context across sessions for recurring topics. Default: off.

---

## Example — a hard question through the pipeline

**User query:** *"Why does my bash script fail silently when I use pipefail inside a function called in a subshell?"*

**PLANNER output:**
```json
{
  "assumptions": [
    "User is on bash (not sh or zsh)",
    "pipefail is being set with set -o pipefail inside the function"
  ],
  "constraints": [
    "Must explain why it fails SILENTLY, not just why it fails",
    "Must distinguish inheritance of shell options in subshells"
  ],
  "steps": [
    "Clarify how set -o options propagate into subshells",
    "Explain when a function runs in a subshell (vs current shell)",
    "Show the specific pipefail + subshell interaction",
    "Give a minimal reproducer",
    "Offer the fix"
  ]
}
```

**SOLVER output:**
```json
{
  "reasoning": "Subshells inherit shell options like pipefail by default...",
  "raw_solution": "When you call foo | tee log in a subshell, pipefail IS inherited..."
}
```

**CRITIC output:**
```json
{
  "issues": [
    "Solver didn't mention errexit (set -e) interaction — often the real culprit",
    "Reproducer assumes bash 4+; some systems still ship bash 3.2"
  ],
  "corrections": [
    "Add note: errexit + pipefail interact; errexit does NOT trigger on pipe failures without pipefail",
    "Mention bash version check"
  ]
}
```

**FINALIZER output:**
```json
{
  "answer": "The short answer: `set -o pipefail` IS inherited into subshells, but `set -e` is NOT fully respected across pipelines without also setting pipefail. ..."
}
```

---

## Invocation

### Standalone (Python CLI)
```bash
python3 ~/scripts/sensei_reasoning_loop.py "your query here"
python3 ~/scripts/sensei_reasoning_loop.py --mode deep "your query"
python3 ~/scripts/sensei_reasoning_loop.py --planner qwen2.5:14b "your query"
```

### From Sensei (prefix)
```
think: why does my bash script fail silently when using pipefail in a subshell?
```

### Programmatic
```python
from sensei_reasoning_loop import run_reasoning_loop

result = run_reasoning_loop("...", mode="standard")
print(result["answer"])
print(result["stages"]["critic"])  # inspect any intermediate stage
```

---

## When to use (and when NOT to)

**Use it for:**
- Multi-part technical questions
- Math / logic with traps
- Design decisions with trade-offs
- Anything where the first-pass answer of a small model tends to miss a case

**Don't use it for:**
- Chit-chat / short clarifications (overkill — adds 60-180s of latency)
- Shell execution tasks (use `loop:` instead — that one runs commands and critiques the results)
- Pure lookups (memory recall handles those faster)

---

## Performance on CPU-only i7-6700T, 15 GB RAM

| Mode | Stages | Expected wall-clock (qwen2.5:7b ~5 tok/s) |
|---|---|---|
| fast | 3 | 60–120 s |
| standard | 4 | 80–180 s |
| deep | 6 | 150–300 s |

On a 32 GB upgrade with qwen2.5:14b: 1.5–2× slower per stage but noticeably better critic catches.

---

## Failure modes + handling

| Failure | Behavior |
|---|---|
| Stage model times out | orchestrator records `(timeout)` for that stage and passes empty string to next |
| Model returns non-JSON | fall back to raw text; mark `parsed: false` |
| Ollama unreachable | abort, print "ollama offline — reasoning loop needs local inference" |
| User Ctrl+C mid-run | clean stop; partial result returned with `stages_completed` list |

---

## Design philosophy

Parameter scale is one axis. **Structure** is another. A 7B model forced to plan → solve → critique → finalize often beats the same 7B running single-pass, because each stage has a narrower job and less cognitive load per step.

This is Claude's pattern — multi-step cognition — applied deliberately to small local models. We can't make Qwen 7B think like Claude. But we can make Qwen 7B do what Claude does: **stop, plan, check its own work, refine.**

That's Sensei Reasoning Loop.
