#!/usr/bin/env python3
"""sensei_3file/orchestrator.py — minimum-viable harness for the
3-file external-scaffolding pattern described in `planner bm`
(Gemini brainstorm, 2026-05-19).

Architecture:
    master_plan.md   — roadmap, owned by the planner. Read-only to the executor.
    active_step.txt  — the single current task, isolated for the 3B brain.
    system_log.txt   — append-only journal of what got done.

Loop per step:
    1. Find next unchecked `- [ ]` step in master_plan.md.
    2. Compose active_step.txt from that step's metadata (objective, target file,
       verification, optional dependency block).
    3. Call qwen2.5:3b via Ollama HTTP API with sensei_executor_prompt.md as system.
    4. Parse FILE: ... ===BEGIN=== ... ===END=== LOG: ... block out of the response.
    5. Write the target file.
    6. Run the verification command. Pass → tick step in master_plan.md, append to
       system_log.txt. Fail → append error to active_step.txt and retry (up to 3).
    7. On 3 failures: dump current task + last error into stuck_step.txt and halt.

This script runs ONE step per invocation by default. Use --max-steps to run several.
NO test runs against a real plan tonight — this is infrastructure only.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "qwen2.5:3b"
SCRIPT_DIR = Path(__file__).resolve().parent
EXECUTOR_PROMPT_PATH = SCRIPT_DIR / "sensei_executor_prompt.md"

# ----- regex anchors --------------------------------------------------------

# Matches one step block in master_plan.md:
#   - [ ] Step 7: Short action
#     - Context/Objective: ...
#     - File to target: `path/to/file`
#     - Verification: <command>
#     - Dependency: (optional, multi-line until next step)
STEP_HEADER_RE = re.compile(
    r"^- \[(?P<state>[ x])\] Step (?P<num>\d+):\s*(?P<title>.+?)\s*$"
)
META_LINE_RE = re.compile(
    r"^\s+-\s+(?P<key>Context/Objective|File to target|Verification|Dependency)\s*:\s*(?P<val>.*?)\s*$",
    re.IGNORECASE,
)


# ----- master_plan.md parsing ----------------------------------------------

def parse_steps(plan_text: str) -> list[dict]:
    """Return a list of step dicts. Each dict has:
        num, state ('x' or ' '), title, raw_lines, meta {context, file, verification, dependency}
    """
    lines = plan_text.splitlines()
    steps = []
    i = 0
    while i < len(lines):
        m = STEP_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        step = {
            "num": int(m.group("num")),
            "state": m.group("state"),
            "title": m.group("title"),
            "header_line": i,
            "meta": {},
            "raw_lines": [lines[i]],
        }
        i += 1
        while i < len(lines):
            if STEP_HEADER_RE.match(lines[i]) or (
                lines[i].startswith("## ") or lines[i].startswith("# ")
            ):
                break
            step["raw_lines"].append(lines[i])
            mm = META_LINE_RE.match(lines[i])
            if mm:
                key = mm.group("key").lower().replace("/", "_").replace(" ", "_")
                # Normalise: "context/objective" -> "context_objective", "file to target" -> "file_to_target"
                step["meta"][key] = mm.group("val").strip().strip("`")
            i += 1
        steps.append(step)
    return steps


def find_next_open_step(steps: list[dict]) -> dict | None:
    for s in steps:
        if s["state"] == " ":
            return s
    return None


# ----- active_step.txt composition -----------------------------------------

def compose_active_step(step: dict, retry_error: str | None = None) -> str:
    meta = step["meta"]
    obj = meta.get("context_objective", step["title"])
    target = meta.get("file_to_target", "(unspecified)")
    verify = meta.get("verification", "(no verification specified)")
    dep = meta.get("dependency", "")
    parts = [
        f"# CURRENT TASK: Step {step['num']} — {step['title']}",
        "=" * 64,
    ]
    if dep:
        parts += [
            "CRITICAL DEPENDENCY DATA (carried from earlier step):",
            dep,
            "=" * 64,
        ]
    parts += [
        f"OBJECTIVE:",
        obj,
        "",
        f"TARGET FILE:",
        target,
        "",
        f"VERIFICATION (the harness will run this — do not run it yourself):",
        verify,
        "",
        "CONSTRAINTS:",
        "- Modify only the TARGET FILE.",
        "- Output exactly one FILE: block per the system prompt.",
        "- Do not add commentary outside the FILE / ===BEGIN=== / ===END=== / LOG block.",
    ]
    if retry_error:
        parts += [
            "",
            "=" * 64,
            "PREVIOUS ATTEMPT FAILED VERIFICATION. ERROR OUTPUT:",
            retry_error,
            "Try a different coding approach.",
        ]
    return "\n".join(parts) + "\n"


# ----- Ollama executor call -------------------------------------------------

def call_executor(system_prompt: str, user_prompt: str, model: str) -> str:
    payload = {
        "model": model,
        "system": system_prompt,
        "prompt": user_prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 4096},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result.get("response", "")


# ----- response parsing -----------------------------------------------------

DIRECTIVE_RE = re.compile(
    r"FILE:\s*(?P<path>.+?)\s*\n===BEGIN===\s*\n(?P<content>.*?)\n===END===\s*\n+LOG:\s*(?P<log>.+?)\s*$",
    re.DOTALL,
)


def parse_directive(response: str) -> dict | None:
    m = DIRECTIVE_RE.search(response)
    if not m:
        return None
    return {
        "path": m.group("path").strip().strip("`"),
        "content": m.group("content"),
        "log": m.group("log").strip(),
    }


# ----- verification ---------------------------------------------------------

def run_verification(verify_cmd: str, workdir: Path) -> tuple[bool, str]:
    if not verify_cmd or verify_cmd.lower().startswith("(no"):
        return True, "(no verification command — auto-pass)"
    try:
        proc = subprocess.run(
            verify_cmd,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "verification timed out (120s)"
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output


# ----- master_plan mutation -------------------------------------------------

def tick_step(plan_path: Path, step_num: int) -> None:
    text = plan_path.read_text()
    pattern = re.compile(rf"^- \[ \] Step {step_num}:", re.MULTILINE)
    new_text, n = pattern.subn(f"- [x] Step {step_num}:", text, count=1)
    if n == 0:
        raise RuntimeError(f"could not tick step {step_num} — header not found")
    plan_path.write_text(new_text)


def append_log(log_path: Path, step_num: int, step_title: str, log_line: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry = (
        f"[{ts}] Step {step_num} — {step_title}\n"
        f"  Impact: {log_line}\n"
    )
    with log_path.open("a") as f:
        f.write(entry)


def write_stuck(stuck_path: Path, step: dict, last_error: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    stuck_path.write_text(
        f"# STUCK at {ts}\n"
        f"Step {step['num']}: {step['title']}\n\n"
        f"## Last verifier output\n{last_error}\n\n"
        f"## Step metadata\n"
        + "\n".join(f"- {k}: {v}" for k, v in step["meta"].items())
        + "\n"
    )


# ----- main loop ------------------------------------------------------------

def run_one_step(workdir: Path, model: str, max_retries: int) -> str:
    """Execute one step from master_plan.md. Returns status string."""
    plan_path = workdir / "master_plan.md"
    active_path = workdir / "active_step.txt"
    log_path = workdir / "system_log.txt"
    stuck_path = workdir / "stuck_step.txt"

    if not plan_path.exists():
        return f"ERR: no master_plan.md in {workdir}"

    steps = parse_steps(plan_path.read_text())
    step = find_next_open_step(steps)
    if step is None:
        return "DONE: no open steps remaining in master_plan.md"

    system_prompt = EXECUTOR_PROMPT_PATH.read_text()
    last_error = None

    for attempt in range(1, max_retries + 1):
        active = compose_active_step(step, retry_error=last_error)
        active_path.write_text(active)

        try:
            response = call_executor(system_prompt, active, model)
        except Exception as e:
            return f"ERR: Ollama call failed — {e}"

        directive = parse_directive(response)
        if not directive:
            last_error = f"model output did not match FILE/===BEGIN===/===END===/LOG format.\nRaw response (truncated):\n{response[:500]}"
            continue

        target_path = workdir / directive["path"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(directive["content"])

        verify_cmd = step["meta"].get("verification", "")
        ok, output = run_verification(verify_cmd, workdir)
        if ok:
            tick_step(plan_path, step["num"])
            append_log(log_path, step["num"], step["title"], directive["log"])
            active_path.write_text("")  # clear sandbox
            return f"PASS: step {step['num']} — {step['title']}"
        last_error = output

    write_stuck(stuck_path, step, last_error or "(no error captured)")
    return f"HALT: step {step['num']} failed verification after {max_retries} attempts. See stuck_step.txt."


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workdir", default=".", help="Project workspace (contains master_plan.md, etc.)")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model tag (default qwen2.5:3b)")
    p.add_argument("--max-retries", type=int, default=3, help="Verification retries per step (default 3)")
    p.add_argument("--max-steps", type=int, default=1, help="Stop after this many successful steps (default 1)")
    args = p.parse_args()

    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        print(f"ERR: workdir not found: {workdir}", file=sys.stderr)
        return 2

    for i in range(args.max_steps):
        status = run_one_step(workdir, args.model, args.max_retries)
        print(status)
        if status.startswith(("DONE", "HALT", "ERR")):
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
