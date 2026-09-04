#!/usr/bin/env python3
"""
Sensei Reasoning Loop — Planner · Solver · Critic · Finalizer
─────────────────────────────────────────────────────────────
A 4-stage reasoning pipeline for local or cloud models.
Forces multi-pass structured cognition so any model can produce
Claude-like multi-step reasoning output.

Model-agnostic: accepts any model identifier your master_ai.py router
supports (Ollama local names, cloud lanes like 'opencode', 'nemotron',
OpenRouter slugs like 'anthropic/claude-3.5-sonnet').

Standalone use:
  python3 sensei_reasoning_loop.py "your query"
  python3 sensei_reasoning_loop.py --mode deep "your query"
  python3 sensei_reasoning_loop.py --planner qwen2.5:14b --solver master-ai "your query"
  python3 sensei_reasoning_loop.py --planner nemotron --solver nemotron "your query"

Programmatic use:
  from sensei_reasoning_loop import run_reasoning_loop
  out = run_reasoning_loop("your query", mode="standard")
  print(out["answer"])
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

STAGE_TIMEOUT = 240        # seconds per stage
STAGE_NUM_PREDICT = 900    # max tokens per stage

# ── Prompt templates ──────────────────────────────────────────
# Each stage has a system prompt (role) and a user prompt (task).
# Templates use {query} and {prior} placeholders.

PROMPT_PLANNER_SYS = """You are the PLANNER in a four-stage reasoning pipeline.

Your ONLY job is to decompose the user's question. You do NOT answer it.
Produce a JSON object with three keys:
  assumptions  — a list of facts you are assuming about the question
  constraints  — a list of what the answer must satisfy
  steps        — a list of 3 to 6 concrete reasoning steps (NOT solutions)

Respond with ONLY the JSON object. No prose before or after."""

PROMPT_PLANNER_USER = """USER QUESTION:
{query}

Produce the planning JSON now."""

PROMPT_SOLVER_SYS = """You are the SOLVER in a four-stage reasoning pipeline.
The PLANNER has already decomposed the question. Your job is to execute the
plan: work through each step, show your reasoning, and produce a complete
proposed solution.

Produce a JSON object with two keys:
  reasoning     — concise working notes and rationale; markdown allowed
  raw_solution  — the proposed answer, complete but not yet polished

Respond with ONLY the JSON object. No prose before or after."""

PROMPT_SOLVER_USER = """USER QUESTION:
{query}

PLANNER OUTPUT:
{prior}

Execute the plan now. Show your work, then produce the raw solution."""

PROMPT_CRITIC_SYS = """You are the CRITIC in a four-stage reasoning pipeline.
The SOLVER produced a proposed answer. Your job is to review it ruthlessly
and find its weaknesses BEFORE it reaches the user.

Look for:
  - logical errors or inconsistencies
  - missing edge cases or assumptions not covered
  - weak reasoning that should be tightened
  - factual claims that look wrong
  - places the solver drifted from the original question

Produce a JSON object with two keys:
  issues       — a list of concrete problems found
  corrections  — a list of specific fixes the finalizer should apply

If the solver output is genuinely good, return empty lists. Do not invent
problems. Respond with ONLY the JSON object."""

PROMPT_CRITIC_USER = """ORIGINAL USER QUESTION:
{query}

PLANNER OUTPUT:
{plan_json}

SOLVER OUTPUT:
{solver_json}

Critique the solver output now."""

PROMPT_FINALIZER_SYS = """You are the FINALIZER in a four-stage reasoning pipeline.
The SOLVER produced a raw answer. The CRITIC flagged issues and corrections.
Your job is to produce the CLEAN, USER-FACING final answer.

Rules:
  - Apply every correction from the critic.
  - Remove reasoning scaffolding, step labels, and internal markers.
  - Write it as if this is the ONLY thing the user sees.
  - Be direct and complete. No meta commentary.
  - NEVER begin a line with RUN:, READ:, CREATE:, EDIT:, THINK:, or DONE:
    — those are reserved directive prefixes that trigger command execution
    elsewhere in Sensei. If you want to SHOW a shell command as an example,
    put it inside a ``` code fence or prefix it with a space so it is text,
    not an instruction.

Produce a JSON object with ONE key:
  answer — the final answer as a string (markdown allowed)

Respond with ONLY the JSON object."""

PROMPT_FINALIZER_USER = """ORIGINAL USER QUESTION:
{query}

SOLVER OUTPUT:
{solver_json}

CRITIC OUTPUT:
{critic_json}

Produce the final clean answer now."""


def _model_chat(model: str, system: str, user: str,
                timeout: int = STAGE_TIMEOUT,
                num_predict: int = STAGE_NUM_PREDICT) -> tuple[str, float]:
    """Model-agnostic chat call.

    If master_ai.py is available in the same repo, route through its
    ask_model_router(). Otherwise fall back to a direct Ollama /api/chat call
    for local models only."""
    t0 = time.time()
    messages = [{"role": "system", "content": system},
                {"role": "user",   "content": user}]

    # Try master_ai's router first (preferred — gives cloud + aliases)
    try:
        import master_ai
        if hasattr(master_ai, "ask_model_router"):
            text, elapsed = master_ai.ask_model_router(messages, model=model,
                                                       max_tokens=num_predict)
            if text:
                return text, elapsed
    except Exception:
        pass

    # Fallback: direct Ollama call for local models
    try:
        import urllib.request
        import urllib.error
        body = json.dumps({
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": num_predict, "temperature": 0.2},
            "keep_alive": "30m",
        }).encode()
        req = urllib.request.Request(
            "http://localhost:11434/api/chat", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        content = (data.get("message") or {}).get("content", "").strip()
        return content, round(time.time() - t0, 2)
    except urllib.error.URLError as e:
        return f"(model unreachable: {e.reason})", round(time.time() - t0, 2)
    except Exception as e:
        return f"(model error: {e})", round(time.time() - t0, 2)


# ── JSON extraction (tolerant — small models add prose sometimes) ──
def _parse_json_lenient(text: str) -> tuple[dict | None, str]:
    """Attempt to extract a JSON object from the model's reply.
    Returns (parsed_dict_or_None, raw_text). If parsing fails, caller
    still gets the raw text so the pipeline can degrade gracefully."""
    if not text:
        return None, ""
    # Try full text first
    try:
        return json.loads(text), text
    except Exception:
        pass
    # Try to find {...} block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0)), text
        except Exception:
            pass
    # Try to find a fenced code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1)), text
        except Exception:
            pass
    return None, text


# ── Stage functions ───────────────────────────────────────────
def plan_stage(query: str, model: str) -> dict:
    """PLANNER: decompose, don't solve. Returns the full stage record."""
    content, elapsed = _model_chat(model, PROMPT_PLANNER_SYS,
                                 PROMPT_PLANNER_USER.format(query=query))
    parsed, raw = _parse_json_lenient(content)
    return {
        "model": model, "elapsed_s": elapsed, "raw": raw,
        "parsed": parsed is not None,
        "json": parsed or {"assumptions": [], "constraints": [],
                            "steps": [raw] if raw else []},
    }


def solve_stage(query: str, plan: dict, model: str) -> dict:
    """SOLVER: execute the plan, show work, produce raw solution."""
    prior = json.dumps(plan["json"], indent=2)
    content, elapsed = _model_chat(model, PROMPT_SOLVER_SYS,
                                 PROMPT_SOLVER_USER.format(query=query, prior=prior),
                                 num_predict=1400)  # more room for solving
    parsed, raw = _parse_json_lenient(content)
    return {
        "model": model, "elapsed_s": elapsed, "raw": raw,
        "parsed": parsed is not None,
        "json": parsed or {"reasoning": raw, "raw_solution": raw},
    }


def critique_stage(query: str, plan: dict, solver: dict, model: str) -> dict:
    """CRITIC: find issues + corrections. May return empty lists if clean."""
    content, elapsed = _model_chat(
        model, PROMPT_CRITIC_SYS,
        PROMPT_CRITIC_USER.format(
            query=query,
            plan_json=json.dumps(plan["json"], indent=2),
            solver_json=json.dumps(solver["json"], indent=2),
        ),
    )
    parsed, raw = _parse_json_lenient(content)
    return {
        "model": model, "elapsed_s": elapsed, "raw": raw,
        "parsed": parsed is not None,
        "json": parsed or {"issues": [], "corrections": []},
    }


def finalize_stage(query: str, solver: dict, critic: dict | None, model: str) -> dict:
    """FINALIZER: clean user-facing answer. Applies critic's corrections.
    If the answer appears truncated, runs a continuation pass."""
    critic_json = json.dumps(critic["json"] if critic else
                              {"issues": [], "corrections": []}, indent=2)

    def _run_finalizer(user_content: str) -> tuple[str, float]:
        return _model_chat(
            model, PROMPT_FINALIZER_SYS,
            user_content,
            num_predict=2400,
        )

    user_content = PROMPT_FINALIZER_USER.format(
        query=query,
        solver_json=json.dumps(solver["json"], indent=2),
        critic_json=critic_json,
    )
    content, elapsed = _run_finalizer(user_content)
    parsed, raw = _parse_json_lenient(content)
    answer = (parsed or {}).get("answer", raw)

    # Continuation pass if answer looks truncated
    for _ in range(2):
        if answer and not _looks_complete(answer):
            continuation_prompt = (
                "Continue exactly where the previous answer was cut off. "
                "Do not repeat what was already written. Finish the answer cleanly.\n\n"
                f"PREVIOUS ANSWER (truncated):\n{answer}\n\n"
                "Continue now."
            )
            cont, cont_elapsed = _model_chat(model, PROMPT_FINALIZER_SYS,
                                             continuation_prompt,
                                             num_predict=2400)
            elapsed += cont_elapsed
            if cont:
                # Try to extract just the answer text; if it comes back as JSON, use that
                cp, _ = _parse_json_lenient(cont)
                cont_text = (cp or {}).get("answer", cont)
                # Avoid duplicating the tail
                answer = _merge_continuation(answer, cont_text)

    return {
        "model": model, "elapsed_s": elapsed, "raw": raw,
        "parsed": parsed is not None,
        "json": {"answer": answer},
        "answer": answer,
    }


def _looks_complete(text: str) -> bool:
    """Heuristic: detect likely truncation mid-sentence or mid-block."""
    if not text:
        return False
    # Strip trailing whitespace
    t = text.rstrip()
    # If it ends with a sentence terminator or markdown closing, it's probably complete
    if t.endswith((".", "!", "?", '"', "'", "```", "</", "---", "===", ")", "]", "}")):
        return True
    # If the last line starts a list/table/code block and never closes, likely truncated
    last_line = t.splitlines()[-1] if t else ""
    if last_line.strip() in ("-", "*", "|", "```", "###", "##", "#"):
        return False
    # Mid-sentence markers
    if t.endswith((",", ":", ";", "(", "[", "{", " ")):
        return False
    return True


def _merge_continuation(original: str, continuation: str) -> str:
    """Merge continuation text without duplicating the overlapping tail of original."""
    if not continuation:
        return original
    # Normalize whitespace for overlap detection
    orig_tail = original[-200:].lstrip()
    cont_head = continuation[:200].lstrip()
    # If continuation starts with same text, skip the duplicate prefix
    if orig_tail and cont_head.startswith(orig_tail):
        return original + continuation[len(orig_tail):]
    return original + "\n\n" + continuation


# ── Orchestrator ──────────────────────────────────────────────
def run_reasoning_loop(query: str, *,
                       mode: str = "standard",
                       models: dict | None = None,
                       memory_file: str | None = None,
                       progress: bool = True) -> dict:
    """Run the full pipeline.

    mode:
      'fast'     — planner → solver → finalizer (critic skipped)
      'standard' — all four stages (default)
      'deep'     — all four + a second solver-critic refinement pass
      'max'      — all four + mandatory second solver-critic refinement pass

    models: optional override per stage. Stage keys are:
      planner, solver, critic, finalizer.
      Any missing stage falls back to the master-ai default model.

    memory_file: optional path to a .jsonl store of prior loops; the
      orchestrator prepends the last 3 (query, answer) pairs to the
      planner context. Omit to disable memory persistence.

    Returns a dict:
      {
        'query': str,
        'mode': str,
        'stages': {'planner': {...}, 'solver': {...}, 'critic': {...}, 'finalizer': {...}},
        'answer': str,
        'total_elapsed_s': float,
        'stages_completed': [str, ...],
      }
    """
    if mode not in ("fast", "standard", "deep", "max"):
        raise ValueError(f"mode must be fast|standard|deep|max (got {mode})")

    # Determine a sensible default model from master_ai if available
    default_model = None
    try:
        import master_ai
        default_model = (master_ai.PINNED_MODEL
                         or master_ai.MODELS.get("master")
                         or "master-ai")
    except Exception:
        default_model = "master-ai"

    mdl = {
        "planner":   default_model,
        "solver":    default_model,
        "critic":    default_model,
        "finalizer": default_model,
    }
    if models:
        mdl.update({k: v for k, v in models.items() if v})

    def _say(msg: str) -> None:
        if progress:
            print(msg, flush=True)

    # Prepend memory context to the query if a memory file is configured
    full_query = query
    if memory_file and os.path.exists(memory_file):
        try:
            recent = []
            with open(memory_file) as f:
                for line in f:
                    try:
                        recent.append(json.loads(line))
                    except Exception:
                        pass
            if recent:
                tail = recent[-3:]
                ctx = "\n".join(
                    f"- prior Q: {r.get('query','')}\n  prior A: {r.get('answer','')[:200]}"
                    for r in tail
                )
                full_query = f"Context from prior reasoning loops:\n{ctx}\n\nCURRENT QUESTION:\n{query}"
        except Exception:
            pass

    result: dict[str, Any] = {
        "query": query, "mode": mode, "stages": {},
        "stages_completed": [], "total_elapsed_s": 0.0,
        "answer": "",
    }
    t0 = time.time()

    try:
        # Stage 1: plan
        _say(f"🧠 [1/4] PLANNER ({mdl['planner']})...")
        plan = plan_stage(full_query, mdl["planner"])
        result["stages"]["planner"] = plan
        result["stages_completed"].append("planner")
        _say(f"    {plan['elapsed_s']}s · parsed={plan['parsed']} · "
             f"{len(plan['json'].get('steps', []))} steps")

        # Stage 2: solve
        _say(f"🧠 [2/4] SOLVER ({mdl['solver']})...")
        solver = solve_stage(full_query, plan, mdl["solver"])
        result["stages"]["solver"] = solver
        result["stages_completed"].append("solver")
        _say(f"    {solver['elapsed_s']}s · parsed={solver['parsed']}")

        # Stage 3: critique (fast mode skips)
        critic = None
        if mode != "fast":
            _say(f"🧠 [3/4] CRITIC ({mdl['critic']})...")
            critic = critique_stage(full_query, plan, solver, mdl["critic"])
            result["stages"]["critic"] = critic
            result["stages_completed"].append("critic")
            issues_n = len(critic["json"].get("issues", []))
            _say(f"    {critic['elapsed_s']}s · parsed={critic['parsed']} · "
                 f"{issues_n} issue{'s' if issues_n != 1 else ''} raised")

            # Deep/max mode: run a second solver pass informed by the critic,
            # then a second critique, then finalize. Deep only spends the
            # extra pass when the critic found issues; max always spends it.
            if mode in ("deep", "max") and (issues_n > 0 or mode == "max"):
                _say(f"🧠 [{mode}] SOLVER pass 2 (refining from critic)...")
                # Feed the prior solver output + critic corrections back as
                # "prior" so the solver can refine.
                refine_prior = {
                    "original_plan": plan["json"],
                    "previous_solution": solver["json"],
                    "critic_issues": critic["json"].get("issues", []),
                    "critic_corrections": critic["json"].get("corrections", []),
                }
                refined_plan_shim = {"json": refine_prior}
                solver = solve_stage(full_query, refined_plan_shim, mdl["solver"])
                result["stages"]["solver_refined"] = solver
                result["stages_completed"].append("solver_refined")
                _say(f"    {solver['elapsed_s']}s · parsed={solver['parsed']}")

                _say(f"🧠 [{mode}] CRITIC pass 2...")
                critic = critique_stage(full_query, plan, solver, mdl["critic"])
                result["stages"]["critic_refined"] = critic
                result["stages_completed"].append("critic_refined")
                _say(f"    {critic['elapsed_s']}s")

        # Stage 4: finalize
        _say(f"🧠 [4/4] FINALIZER ({mdl['finalizer']})...")
        final = finalize_stage(full_query, solver, critic, mdl["finalizer"])
        result["stages"]["finalizer"] = final
        result["stages_completed"].append("finalizer")
        _say(f"    {final['elapsed_s']}s · parsed={final['parsed']}")

        result["answer"] = final["answer"]
    except KeyboardInterrupt:
        _say("\n  ⚠ interrupted — returning partial result")

    result["total_elapsed_s"] = round(time.time() - t0, 2)

    # Persist to memory file if configured
    if memory_file:
        try:
            Path(memory_file).parent.mkdir(parents=True, exist_ok=True)
            with open(memory_file, "a") as f:
                f.write(json.dumps({
                    "ts": int(time.time()),
                    "query": query,
                    "mode": mode,
                    "answer": result["answer"],
                    "elapsed_s": result["total_elapsed_s"],
                }) + "\n")
        except Exception:
            pass

    return result


# ── CLI ───────────────────────────────────────────────────────
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Sensei Reasoning Loop — Planner/Solver/Critic/Finalizer"
    )
    ap.add_argument("query", nargs="+", help="the user question")
    ap.add_argument("--mode", choices=("fast", "standard", "deep", "max"),
                    default="standard")
    ap.add_argument("--planner",   default=None)
    ap.add_argument("--solver",    default=None)
    ap.add_argument("--critic",    default=None)
    ap.add_argument("--finalizer", default=None)
    ap.add_argument("--memory", default=None,
                    help="optional .jsonl file to persist loops across runs")
    ap.add_argument("--json", action="store_true",
                    help="emit the full result dict as JSON (for piping)")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-stage progress lines")
    args = ap.parse_args()

    query = " ".join(args.query).strip()
    models = {
        "planner": args.planner, "solver": args.solver,
        "critic": args.critic,  "finalizer": args.finalizer,
    }
    # Drop None values so orchestrator uses its own default
    models = {k: v for k, v in models.items() if v}

    out = run_reasoning_loop(query, mode=args.mode, models=models,
                              memory_file=args.memory, progress=not args.quiet)

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print()
        print("─" * 60)
        print("  FINAL ANSWER")
        print("─" * 60)
        print(out["answer"] or "(no answer — pipeline produced no final output)")
        print()
        print(f"  total: {out['total_elapsed_s']}s  stages: {', '.join(out['stages_completed'])}")
    return 0 if out["answer"] else 1


if __name__ == "__main__":
    sys.exit(_main())
