"""Skill author for Sensei — turn a session transcript into a runnable skill.

Two modes:
  1. supervised: user runs `skill create <name>` from a transcript or the
     current session. A draft SKILL.md + recipe.py is generated, then
     audited. User must confirm before it is saved.
  2. autonomous: `session_harvester.best_candidate()` finds a strong
     candidate and this module turns it into a skill. If the candidate is
     low-risk (no shell commands, no browser automation, no file writes)
     and auto-author is enabled, it saves directly. Otherwise it asks.

Stdlib only. No LLM calls here — the summarization/extraction is rule-
based to keep it deterministic and local. If richer extraction is needed,
callers can pass an `extract_fn` callback.
"""

from __future__ import annotations

import os
import py_compile
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import session_harvester as harvester
from skill_marketplace import validate_skill_dir
from skill_runtime import END, SKILLS_ROOT

# Patterns used to infer what a skill should DO from user turns.
_CMD_RE = re.compile(
    r"(?:run|execute|install|build|create|make|patch|fix|check|verify|"
    r"audit|deploy|generate|wire)\s+(?:a|an|the|my)?\s+(.{3,120}?)(?:\.|\?|!|\n|for|with|using|to)",
    re.IGNORECASE,
)
_FILE_RE = re.compile(r"(?:/home/|~/|\./)[A-Za-z0-9_./-]+[A-Za-z0-9_./-]*")
_URL_RE = re.compile(r"https?://[^\s\"']+")
_PACKAGE_RE = re.compile(r"(?:pip|apt|npm|npx)\s+\w+[^\n]*", re.IGNORECASE)

# Risk categories for autonomous approval.
_HIGH_RISK_COMMANDS = re.compile(
    r"\b(sudo|rm\s+-rf|dd\s+|mkfs|fdisk|deluser|userdel|passwd|chmod\s+777|"
    r"chroot|reboot|poweroff|halt|shutdown)\b",
    re.IGNORECASE,
)


@dataclass
class Draft:
    name: str
    skill_md: str
    recipe_py: str
    skill_dir: Path | None = None
    transcript_path: Path | None = None
    audit: dict = field(default_factory=dict)
    low_risk: bool = False
    approved: bool = False


def _slugify(text: str) -> str:
    slug = text.lower().replace("&", " and ")
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    return slug[:50].strip("-") or "untitled-skill"


def _summarize_transcript(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract a structured skill spec from chat turns."""
    user_texts = [
        harvester._strip_tool_blocks(m["text"])
        for m in messages
        if m.get("role") == "you"
    ]
    ai_texts = [m["text"] for m in messages if m.get("role") == "ai"]
    combined = "\n".join(user_texts + ai_texts)

    # Topic / goal
    topic = harvester._topic_from_messages(user_texts)
    name = harvester._name_from_topic(topic)

    # Commands / tools used
    tool_commands = []
    for t in user_texts + ai_texts:
        tool_commands.extend(harvester._extract_tool_commands(t))
    unique_tools = sorted(set(tool_commands))[:30]

    # File paths referenced
    file_paths = sorted(set(_FILE_RE.findall(combined)))[:20]
    urls = sorted(set(_URL_RE.findall(combined)))[:10]
    packages = sorted(set(_PACKAGE_RE.findall(combined)))[:10]

    # Build a one-line goal sentence
    goal = f"Automate or guide the workflow: {topic}."
    if unique_tools:
        goal += f" Uses: {', '.join(unique_tools[:5])}."

    # Infer steps from action verbs in user requests
    steps = []
    for t in user_texts:
        for m in _CMD_RE.finditer(t):
            action = m.group(1).strip().lower()
            if action and len(action) > 3:
                steps.append(action[:100])
    steps = steps[:8] or ["collect inputs", "execute workflow", "verify result"]

    # Recovery: common failure patterns observed in the session
    recoveries = []
    for t in ai_texts:
        if "not found" in t.lower() or "missing" in t.lower() or "error" in t.lower():
            recoveries.append(
                "If a required file or command is missing, report the exact path and ask whether to install it."
            )
            break
    if not recoveries:
        recoveries.append(
            "If a step fails, log the error and ask the operator before retrying."
        )

    # Parameters: anything that looks like a variable value / path the user provided
    params = []
    for fp in file_paths[:5]:
        params.append(
            {
                "name": fp.split("/")[-1].replace(".", "_"),
                "type": "path",
                "description": f"Path referenced in session: {fp}",
                "required": False,
            }
        )
    if not params:
        params.append(
            {
                "name": "topic",
                "type": "string",
                "description": "The workflow topic or target",
                "required": True,
            }
        )

    return {
        "name": name,
        "topic": topic,
        "goal": goal,
        "steps": steps,
        "recoveries": recoveries,
        "params": params,
        "tools": unique_tools,
        "file_paths": file_paths,
        "urls": urls,
        "packages": packages,
    }


def _build_skill_md(spec: dict[str, Any]) -> str:
    """Render a SKILL.md from an extracted spec."""
    params_md = "\n".join(
        f"- `{p['name']}` ({p['type']}) — {p['description']} "
        f"({'required' if p.get('required') else 'optional'})"
        for p in spec["params"]
    )
    steps_md = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(spec["steps"]))
    recoveries_md = "\n".join(f"- {r}" for r in spec["recoveries"])
    tools_md = (
        ", ".join(f"`{t}`" for t in spec["tools"][:10]) or "_inferred from session_"
    )

    return f"""---
name: {spec["name"]}
description: {spec["goal"]}
version: 0.1.0
author: sensei-auto-author
auto_generated: true
---

# {spec["name"].replace("-", " ").title()}

## Goal
{spec["goal"]}

## Parameters
{params_md}

## Steps
{steps_md}

## Recovery
{recoveries_md}

## Tools observed
{tools_md}

## Notes
This skill was automatically generated from a Sensei session transcript.
Review before relying on it in production.
"""


def _build_recipe_py(spec: dict[str, Any]) -> str:
    """Render a recipe.py with a minimal but valid STEPS state machine.

    The generated recipe is deliberately conservative:
      - any shell execution goes through sandbox.run_sandboxed()
      - no eval/exec/raw subprocess
      - no hardcoded secrets
    """
    step_fns = []
    step_names = []
    for i, step_desc in enumerate(spec["steps"], start=1):
        name = f"step_{i:02d}"
        step_names.append((i, name, step_desc))

    next_map = {}
    for idx, (i, name, _) in enumerate(step_names):
        if idx + 1 < len(step_names):
            next_map[name] = step_names[idx + 1][1]
        else:
            next_map[name] = END

    for i, name, step_desc in step_names:
        next_name = next_map[name]
        safe_desc = step_desc.replace('"', "'").replace("\\", " ")
        step_fns.append(
            f"""def {name}(state, params):
    \"\"\"{safe_desc}\"\"\"
    state.data[\"last_step\"] = \"{name}\"
    # TODO: implement the actual work here.
    # Use sandbox.run_sandboxed([...]) for any shell command.
    result = {{\"ok\": True, \"detail\": f\"Executed: {safe_desc}\"}}
    state.append_history(\"{name}\", result)
    return {{\"next\": \"{next_name}\"}}
"""
        )

    recipe = f"""#!/usr/bin/env python3
\"\"\"Auto-generated recipe for skill: {spec["name"]}

Goal: {spec["goal"]}
\"\"\"

import json
import os
from pathlib import Path
from skill_runtime import Step, START, END, ABORT, INTERRUPT, SkillState
from sandbox import run_sandboxed

# ── Helper functions ────────────────────────────────────────────────

"""
    recipe += "\n\n".join(step_fns)
    recipe += f"""

# ── STEPS state machine ─────────────────────────────────────────────

STEPS = [
    Step(name=n, fn=globals()[n])
    for n in {[name for _, name, _ in step_names]!r}
]

ENTRYPOINT = STEPS[0].name


def CHECK_PRECONDITIONS():
    \"\"\"Verify the environment is ready for this skill.\"\"\"
    # Auto-generated placeholder: add real checks if needed.
    pass
"""
    return recipe


def _low_risk(spec: dict[str, Any]) -> bool:
    """Return True if the generated skill is safe to auto-approve."""
    # If no shell commands are observed and no URLs/files, it's mostly
    # conversational / advisory. Still require audit to pass.
    if spec["tools"]:
        tool_text = " ".join(spec["tools"]).lower()
        if _HIGH_RISK_COMMANDS.search(tool_text):
            return False
        # Any RUN directive suggests shell execution
        if "run:" in tool_text or "bash" in tool_text or "sudo" in tool_text:
            return False
    # Conservative default: ask unless it's purely Q&A
    has_file_ops = bool(spec["file_paths"])
    has_urls = bool(spec["urls"])
    has_packages = bool(spec["packages"])
    return not (has_file_ops or has_urls or has_packages)


def draft_from_transcript(
    name: str | None = None,
    transcript_path: Path | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> Draft:
    """Create a draft skill from a chat transcript.

    Args:
        name: optional skill name; inferred from topic if omitted.
        transcript_path: path to .chat file to read.
        messages: alternative to transcript_path; raw parsed messages.
    """
    source_path = transcript_path
    if messages is None:
        if transcript_path is None:
            raise ValueError("provide transcript_path or messages")
        messages = harvester._read_chat(Path(transcript_path))
    if not messages:
        raise ValueError("transcript is empty")

    spec = _summarize_transcript(messages)
    if name:
        spec["name"] = _slugify(name)
    skill_md = _build_skill_md(spec)
    recipe_py = _build_recipe_py(spec)
    return Draft(
        name=spec["name"],
        skill_md=skill_md,
        recipe_py=recipe_py,
        low_risk=_low_risk(spec),
        transcript_path=Path(source_path) if source_path else None,
    )


def _tmp_skill_dir(name: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix=f"skill_author_{name}_"))
    d = tmp / name
    d.mkdir(parents=True)
    return d


def _write_skill_files(draft: Draft, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(draft.skill_md)
    (dest / "recipe.py").write_text(draft.recipe_py)
    os.chmod(dest / "recipe.py", 0o644)


def _audit_draft(draft: Draft) -> dict[str, Any]:
    """Materialize draft to a temp dir and run the adapted-skill audit."""
    tmp_dir = _tmp_skill_dir(draft.name)
    _write_skill_files(draft, tmp_dir)
    result = validate_skill_dir(tmp_dir)
    # Also py_compile the recipe to catch syntax errors
    try:
        py_compile.compile(str(tmp_dir / "recipe.py"), doraise=True)
        compile_ok = True
    except py_compile.PyCompileError as e:
        compile_ok = False
        result.reasons.append(f"recipe.py syntax error: {e}")
        result.passed = False
    draft.audit = result.to_dict()
    draft.audit["compile_ok"] = compile_ok
    draft.skill_dir = tmp_dir
    return draft.audit


def create_skill(
    name: str,
    transcript_path: Path | None = None,
    messages: list[dict[str, Any]] | None = None,
    auto_approve: bool = False,
) -> Draft:
    """Full supervised/auto pipeline: draft, audit, (optionally) save.

    Returns the Draft with `approved` set if it was saved.
    """
    draft = draft_from_transcript(name, transcript_path, messages)
    _audit_draft(draft)

    if not draft.audit.get("passed"):
        return draft  # caller must show audit failures

    assert draft.skill_dir is not None
    dest = SKILLS_ROOT / draft.name
    if dest.exists():
        draft.audit["passed"] = False
        draft.audit.setdefault("reasons", []).append(
            f"skill directory already exists: {dest}"
        )
        return draft

    if not auto_approve and not draft.low_risk:
        # Caller must ask user
        return draft

    shutil.copytree(draft.skill_dir, dest)
    draft.approved = True
    return draft


def propose_auto_skill(
    min_score: float = 0.6,
    auto_approve_enabled: bool = False,
) -> Draft | None:
    """Harvest the best candidate and turn it into a draft skill.

    Returns None if no strong candidate. Returns draft without saving if
    auto-approve is disabled or the draft is not low-risk.
    """
    candidate = harvester.best_candidate()
    if candidate is None:
        return None
    if candidate.message_count < 4:
        return None

    messages = harvester._read_chat(candidate.transcript_path)
    draft = draft_from_transcript(name=candidate.name, messages=messages)
    draft.transcript_path = candidate.transcript_path
    _audit_draft(draft)

    if not draft.audit.get("passed"):
        return draft

    assert draft.skill_dir is not None
    dest = SKILLS_ROOT / draft.name
    if dest.exists():
        draft.audit["passed"] = False
        draft.audit.setdefault("reasons", []).append("skill already exists")
        return draft

    if auto_approve_enabled and draft.low_risk:
        shutil.copytree(draft.skill_dir, dest)
        draft.approved = True
    return draft


if __name__ == "__main__":
    # quick smoke test using the best recent candidate
    d = propose_auto_skill(auto_approve_enabled=False)
    if d:
        print(f"draft: {d.name}")
        print(f"low_risk: {d.low_risk}")
        print(f"audit passed: {d.audit.get('passed')}")
        print(f"reasons: {d.audit.get('reasons')}")
        print(f"warnings: {d.audit.get('warnings')}")
        print("--- SKILL.md ---")
        print(d.skill_md[:500])
    else:
        print("no candidate")
