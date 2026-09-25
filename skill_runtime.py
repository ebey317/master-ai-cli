"""
Master AI skill state-machine runtime — Path A (DIY in our idiom).

A "skill" is a directory at ~/.master_ai_skills/<name>/ containing:
  - SKILL.md   — human-readable spec (preconditions, parameters, steps, recovery)
  - recipe.py  — Python implementation: STEPS list + optional STATE_SHAPE class

A "session" is ONE runtime invocation of a skill. Each step is a function
returning a partial state update plus a "next" key naming the following
step (or sentinel: END / ABORT / INTERRUPT). State is persisted to disk
after each step so interrupted sessions resume cleanly.

Pattern lineage:
  - LangGraph (typed state + reducers + START/END constants + conditional edges)
  - Voyager (skill library at ~/.master_ai_skills/)
  - CodeAct / OpenHands (deterministic step machine, LLM only inside steps)
  - Reflexion (recovery/retry as first-class part of the step shape)

Implementation is in Master AI's idiom: stdlib only (json, os, importlib,
dataclasses, uuid, time). No langgraph import, no browser-use import,
no langchain import. Per Elijah 2026-05-17 PM "Path A: DIY" decision.

Wisdom-compounds principle (per the same session): every session writes
to disk; recipes that learn (e.g. cached answers, recognized ATSes) are
expected to append to their own knowledge files. The runtime doesn't
prescribe the accumulation mechanism — recipes own that.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ─── Constants ──────────────────────────────────────────────────────

SKILLS_ROOT = Path(os.path.expanduser("~/.master_ai_skills"))
DEFAULT_STEP_BUDGET = 50

# Step-return sentinels (named-string convention rather than enum so they
# serialize cleanly to JSON and can appear in SKILL.md as bare text).
START = "__start__"
END = "__end__"
ABORT = "__abort__"
INTERRUPT = "__interrupt__"

log = logging.getLogger("skill_runtime")


# ─── Errors ─────────────────────────────────────────────────────────


class SkillError(Exception):
    """Generic skill-runtime error. Subclasses below for specific cases."""


class SkillNotFound(SkillError):
    pass


class SkillSchemaError(SkillError):
    pass


class PreconditionFailed(SkillError):
    pass


class StepNotFound(SkillError):
    pass


class StepBudgetExceeded(SkillError):
    pass


# ─── Data shapes ────────────────────────────────────────────────────


@dataclass
class Step:
    """One step in a skill recipe.

    fn signature: (state: SkillState, params: dict) -> dict
      Return-dict MUST include a "next" key whose value is either:
        - the name of another step
        - END / ABORT / INTERRUPT sentinel
      Return-dict MAY include "state_update" with partial state to merge.
      Return-dict MAY include "artifact" with named artifacts to store.
    """

    name: str
    fn: Callable[[SkillState, dict], dict]
    description: str = ""
    retry_on_fail: int = 0
    recovery_next: str | None = None  # step to jump to when retries exhausted


@dataclass
class SkillState:
    """Persistent state for a skill session.

    Recipes can stash typed fields in `data` (a free-form dict). Subclassing
    SkillState is NOT required — recipes that need typed fields can just
    document their `data` keys in the SKILL.md spec.

    Reducers (LangGraph terminology):
      - history     : append-only list of {step, result, ts}
      - errors      : append-only list of {step, error, ts}
      - artifacts   : dict keyed by step name (overwrite-on-collision)
      - data        : dict (recipes own the merge semantics in their step fns)
    """

    skill_name: str
    session_id: str
    current_step: str = START
    next_step: str | None = None
    step_count: int = 0
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    done: bool = False
    aborted: bool = False
    interrupt_reason: str | None = None  # set when waiting on operator
    history: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SkillState:
        return cls(**d)

    def append_history(self, step: str, result: dict) -> None:
        self.history.append(
            {
                "step": step,
                "result": _scrub_for_json(result),
                "ts": time.time(),
            }
        )

    def append_error(self, step: str, error: str) -> None:
        self.errors.append(
            {
                "step": step,
                "error": str(error)[:2000],
                "ts": time.time(),
            }
        )


# ─── Loader ─────────────────────────────────────────────────────────


def _skill_dir(name: str) -> Path:
    return SKILLS_ROOT / name


def _validate_step_list(steps: list) -> None:
    """Duck-typed validation. Strict `isinstance(s, Step)` would fail when
    skill_runtime is loaded once as __main__ (CLI entry) and once as a
    regular module (via the recipe's `from skill_runtime import Step`) —
    that yields two distinct Step classes with identical structure. We
    check by attribute shape instead."""
    if not isinstance(steps, list) or not steps:
        raise SkillSchemaError("recipe.STEPS must be a non-empty list of Step objects")
    names = set()
    for s in steps:
        if not (
            hasattr(s, "name") and hasattr(s, "fn") and callable(getattr(s, "fn", None))
        ):
            raise SkillSchemaError(
                f"recipe.STEPS entry is not Step-shaped (missing name/fn/callable): {s!r}"
            )
        if not isinstance(s.name, str) or not s.name:
            raise SkillSchemaError(f"recipe.STEPS entry has invalid name: {s!r}")
        if s.name in names:
            raise SkillSchemaError(f"duplicate step name: {s.name}")
        if s.name in (START, END, ABORT, INTERRUPT):
            raise SkillSchemaError(f"step name conflicts with sentinel: {s.name}")
        names.add(s.name)


def load_skill(name: str) -> dict:
    """Load a skill from ~/.master_ai_skills/<name>/.

    Returns a dict with:
      - name        : the skill name
      - skill_md    : the raw SKILL.md text (or "" if absent)
      - steps       : dict {step_name -> Step}
      - entrypoint  : the name of the first step (recipe.ENTRYPOINT or steps[0])
      - module      : the imported recipe module (kept alive)
    """
    skill_path = _skill_dir(name)
    if not skill_path.is_dir():
        raise SkillNotFound(f"no skill dir at {skill_path}")

    skill_md_path = skill_path / "SKILL.md"
    skill_md = skill_md_path.read_text() if skill_md_path.exists() else ""

    recipe_path = skill_path / "recipe.py"
    if not recipe_path.exists():
        raise SkillSchemaError(f"no recipe.py in {skill_path}")

    spec = importlib.util.spec_from_file_location(
        f"_skill_{name.replace('-', '_')}", recipe_path
    )
    if spec is None or spec.loader is None:
        raise SkillSchemaError(f"could not load recipe.py at {recipe_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise SkillSchemaError(f"recipe.py import failed: {e}") from e

    steps_raw = getattr(module, "STEPS", None)
    if steps_raw is None:
        raise SkillSchemaError(f"recipe.py at {recipe_path} has no STEPS attribute")
    _validate_step_list(steps_raw)

    steps_by_name: dict[str, Step] = {s.name: s for s in steps_raw}
    entrypoint = getattr(module, "ENTRYPOINT", steps_raw[0].name)
    if entrypoint not in steps_by_name:
        raise SkillSchemaError(
            f"ENTRYPOINT={entrypoint!r} not in step list "
            f"({sorted(steps_by_name.keys())})"
        )

    return {
        "name": name,
        "skill_md": skill_md,
        "steps": steps_by_name,
        "entrypoint": entrypoint,
        "module": module,
    }


# ─── Preconditions ──────────────────────────────────────────────────


def check_preconditions(loaded: dict) -> None:
    """Run the recipe's CHECK_PRECONDITIONS hook if defined.

    The hook signature: () -> None  (raises PreconditionFailed on failure).
    Skills without a precondition hook are assumed always-ready.
    """
    mod = loaded["module"]
    check = getattr(mod, "CHECK_PRECONDITIONS", None)
    if check is None:
        return
    try:
        check()
    except PreconditionFailed:
        raise
    except Exception as e:
        raise PreconditionFailed(f"precondition check raised: {e}") from e


# ─── Session persistence ────────────────────────────────────────────


def _sessions_dir(name: str) -> Path:
    p = _skill_dir(name) / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _session_path(name: str, session_id: str) -> Path:
    return _sessions_dir(name) / f"{session_id}.json"


def save_state(state: SkillState) -> None:
    p = _session_path(state.skill_name, state.session_id)
    state.updated_at = time.time()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2, default=_json_default))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def load_state(name: str, session_id: str) -> SkillState:
    p = _session_path(name, session_id)
    if not p.exists():
        raise SkillNotFound(f"no session at {p}")
    return SkillState.from_dict(json.loads(p.read_text()))


def list_sessions(name: str) -> list[dict]:
    d = _sessions_dir(name)
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            out.append(
                {
                    "session_id": data.get("session_id"),
                    "current_step": data.get("current_step"),
                    "done": data.get("done"),
                    "aborted": data.get("aborted"),
                    "interrupt_reason": data.get("interrupt_reason"),
                    "step_count": data.get("step_count", 0),
                    "updated_at": data.get("updated_at"),
                }
            )
        except Exception:
            continue
    return out


# ─── Runner ─────────────────────────────────────────────────────────


def run_skill(
    name: str,
    params: dict | None = None,
    *,
    session_id: str | None = None,
    step_budget: int = DEFAULT_STEP_BUDGET,
    resume: bool = False,
) -> SkillState:
    """Run (or resume) a skill session.

    If resume=True and session_id is given, picks up a saved state.
    Otherwise starts a fresh session.
    """
    loaded = load_skill(name)
    check_preconditions(loaded)

    if resume and session_id:
        state = load_state(name, session_id)
        if state.done or state.aborted:
            return state
    else:
        state = SkillState(
            skill_name=name,
            session_id=session_id or f"{int(time.time())}-{uuid.uuid4().hex[:8]}",
            current_step=loaded["entrypoint"],
            params=dict(params or {}),
        )
        save_state(state)

    steps = loaded["steps"]

    while not state.done and not state.aborted:
        if state.step_count >= step_budget:
            state.aborted = True
            state.interrupt_reason = f"step_budget_exceeded ({step_budget})"
            state.append_error(state.current_step, state.interrupt_reason)
            save_state(state)
            raise StepBudgetExceeded(state.interrupt_reason)

        step_name = state.current_step
        if step_name in (END,):
            state.done = True
            save_state(state)
            break
        if step_name in (ABORT,):
            state.aborted = True
            save_state(state)
            break
        if step_name == INTERRUPT:
            # If we entered the loop on INTERRUPT, treat as wait — caller
            # decides whether to advance state.current_step before resume.
            save_state(state)
            break

        step = steps.get(step_name)
        if step is None:
            state.append_error(step_name, "step not found in recipe")
            state.aborted = True
            save_state(state)
            raise StepNotFound(f"step {step_name!r} not in recipe for {name}")

        result = _run_step_with_retries(step, state)

        # Result shape: {"next": <step_name|sentinel>, "state_update": dict?, "artifact": Any?}
        nxt = result.get("next", END)
        if "state_update" in result and isinstance(result["state_update"], dict):
            state.data.update(result["state_update"])
        if "artifact" in result:
            state.artifacts[step_name] = result["artifact"]
        if result.get("interrupt"):
            state.current_step = INTERRUPT
            state.interrupt_reason = str(
                result.get("interrupt_reason") or "operator_input_required"
            )
            state.append_history(step_name, result)
            state.step_count += 1
            save_state(state)
            break

        state.append_history(step_name, result)
        state.step_count += 1
        state.current_step = nxt
        save_state(state)

        if nxt == END:
            state.done = True
            save_state(state)
            break
        if nxt == ABORT:
            state.aborted = True
            save_state(state)
            break

    return state


def _run_step_with_retries(step: Step, state: SkillState) -> dict:
    last_err = None
    attempts = max(1, 1 + (step.retry_on_fail or 0))
    for attempt in range(attempts):
        try:
            out = step.fn(state, state.params)
            if not isinstance(out, dict):
                raise SkillError(
                    f"step {step.name!r} returned {type(out).__name__}, expected dict"
                )
            return out
        except Exception as e:
            last_err = e
            state.append_error(
                step.name,
                f"attempt {attempt + 1}/{attempts}: {e}\n{traceback.format_exc()[:1200]}",
            )
            time.sleep(min(2**attempt, 8))  # bounded backoff: 1, 2, 4, 8, 8...

    # Retries exhausted. If recipe declared a recovery step, route there;
    # else abort the session.
    if step.recovery_next:
        return {
            "next": step.recovery_next,
            "state_update": {
                "_recovery_from": step.name,
                "_recovery_err": str(last_err),
            },
        }
    return {
        "next": ABORT,
        "state_update": {"_aborted_at": step.name, "_abort_err": str(last_err)},
    }


# ─── Helpers ────────────────────────────────────────────────────────


def _scrub_for_json(obj: Any) -> Any:
    """Make an arbitrary Python object JSON-safe."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _scrub_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_scrub_for_json(v) for v in obj]
    return repr(obj)[:500]


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return repr(obj)[:500]


# ─── CLI / smoke test ───────────────────────────────────────────────


def _print_session_summary(state: SkillState) -> None:
    print(f"skill:        {state.skill_name}")
    print(f"session_id:   {state.session_id}")
    print(f"current_step: {state.current_step}")
    print(f"step_count:   {state.step_count}")
    print(f"done:         {state.done}")
    print(f"aborted:      {state.aborted}")
    if state.interrupt_reason:
        print(f"interrupted:  {state.interrupt_reason}")
    print(f"history len:  {len(state.history)}")
    print(f"errors:       {len(state.errors)}")
    print(f"artifacts:    {list(state.artifacts.keys())}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: skill_runtime.py <skill-name> [<params-json>]", file=sys.stderr)
        sys.exit(2)
    skill_name = sys.argv[1]
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        result = run_skill(skill_name, params)
        _print_session_summary(result)
    except (SkillNotFound, SkillSchemaError, PreconditionFailed) as e:
        print(f"ERROR ({type(e).__name__}): {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"FATAL ({type(e).__name__}): {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
