#!/usr/bin/env python3
"""
Few-shot A/B harness.

Runs each prompt in a prompt-set file through master_ai.ask_local() twice:
once with FEW_SHOT=0 (control) and once with FEW_SHOT=1 (treatment).
Writes a side-by-side markdown report to /home/user/MD/.

Usage:
    python3 ~/scripts/ab_few_shot.py PROMPTS_FILE [REPORT_OUT]

PROMPTS_FILE: path to a text file, one prompt per line, blank lines and
              lines starting with '#' are skipped.
REPORT_OUT:   optional output path. Defaults to
              /home/user/MD/handoff_fewshot_ab_<YYYY-MM-DD>.md

The script:
  1. Captures the existing ~/.master_ai_settings.
  2. For each prompt:
       - sets FEW_SHOT=0, calls ask_local(), times it.
       - sets FEW_SHOT=1, calls ask_local(), times it.
  3. Restores the original settings file at the end (even on exception).
  4. Writes a markdown report with prompt, OFF reply, ON reply, timings,
     and a tally of which side emitted directive shapes.

Tags directive presence so we can see at a glance whether few-shot
helped the qwen2.5:7b "describe vs emit" gap.
"""

import os
import re
import sys
import time
import traceback
from pathlib import Path

SETTINGS = Path.home() / ".master_ai_settings"
MD_DIR = Path("/home/user/MD")


def read_settings():
    if not SETTINGS.exists():
        return []
    return SETTINGS.read_text().splitlines()


def write_settings(lines):
    SETTINGS.write_text("\n".join(lines) + ("\n" if lines and not lines[-1].endswith("\n") else ""))


def set_few_shot(on):
    val = "1" if on else "0"
    lines = read_settings()
    out = []
    replaced = False
    for line in lines:
        if line.strip().startswith("FEW_SHOT="):
            out.append(f"FEW_SHOT={val}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"FEW_SHOT={val}")
    write_settings(out)


DIRECTIVE_RE = re.compile(r"^\s*(RUN|RUNTERM|READ|CREATE|EDIT|REMEMBER|THINK|DONE|PLAN):", re.MULTILINE)


def has_directive(text):
    if not text:
        return False
    return bool(DIRECTIVE_RE.search(text))


def load_prompts(path):
    prompts = []
    for line in Path(path).read_text().splitlines():
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        prompts.append(line)
    return prompts


def run_one(prompt, mode_label):
    import master_ai
    messages = [{"role": "user", "content": prompt}]
    t0 = time.time()
    try:
        reply = master_ai.ask_local(messages)
    except Exception as e:
        reply = f"(EXCEPTION: {e})"
    elapsed = time.time() - t0
    print(f"  {mode_label}: {elapsed:5.1f}s  directive={has_directive(reply)}")
    return reply or "(no response)", elapsed


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    prompts_path = sys.argv[1]
    if not Path(prompts_path).is_file():
        print(f"prompts file not found: {prompts_path}", file=sys.stderr)
        sys.exit(2)

    out_path = (Path(sys.argv[2]) if len(sys.argv) > 2
                else MD_DIR / f"handoff_fewshot_ab_{time.strftime('%Y-%m-%d')}.md")

    prompts = load_prompts(prompts_path)
    if not prompts:
        print("no prompts in file (skipping blanks/#)")
        sys.exit(2)

    sys.path.insert(0, str(Path.home() / "scripts"))
    original_settings = read_settings()

    print(f"prompts: {len(prompts)}")
    print(f"output:  {out_path}")
    print(f"backing up {SETTINGS} (will restore at end)")

    results = []
    try:
        for i, prompt in enumerate(prompts, 1):
            print(f"\n[{i}/{len(prompts)}] {prompt[:70]}")
            set_few_shot(False)
            off_text, off_t = run_one(prompt, "OFF")
            set_few_shot(True)
            on_text, on_t = run_one(prompt, "ON ")
            results.append({
                "prompt": prompt,
                "off_text": off_text, "off_t": off_t,
                "off_dir": has_directive(off_text),
                "on_text": on_text, "on_t": on_t,
                "on_dir": has_directive(on_text),
            })
    finally:
        SETTINGS.write_text("\n".join(original_settings) + "\n")
        print(f"\nrestored {SETTINGS}")

    off_dir = sum(1 for r in results if r["off_dir"])
    on_dir = sum(1 for r in results if r["on_dir"])
    off_chars = sum(len(r["off_text"]) for r in results)
    on_chars = sum(len(r["on_text"]) for r in results)
    off_total_t = sum(r["off_t"] for r in results)
    on_total_t = sum(r["on_t"] for r in results)

    lines = []
    lines.append(f"# Few-shot A/B — {time.strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append("")
    lines.append(f"- prompts: **{len(results)}**")
    lines.append(f"- model: `master-ai` (qwen2.5:7b + Sensei SYSTEM)")
    lines.append(f"- prompts file: `{prompts_path}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| metric | OFF (control) | ON (few-shot) |")
    lines.append("|---|---|---|")
    lines.append(f"| directives emitted | {off_dir}/{len(results)} | {on_dir}/{len(results)} |")
    lines.append(f"| total reply chars  | {off_chars} | {on_chars} |")
    lines.append(f"| total elapsed (s)  | {off_total_t:.1f} | {on_total_t:.1f} |")
    lines.append("")
    lines.append("---")
    for i, r in enumerate(results, 1):
        lines.append("")
        lines.append(f"## Prompt {i}")
        lines.append("")
        lines.append(f"> {r['prompt']}")
        lines.append("")
        lines.append(f"### OFF (control) — {r['off_t']:.1f}s — directive={r['off_dir']}")
        lines.append("")
        lines.append("```")
        lines.append(r["off_text"])
        lines.append("```")
        lines.append("")
        lines.append(f"### ON (few-shot) — {r['on_t']:.1f}s — directive={r['on_dir']}")
        lines.append("")
        lines.append("```")
        lines.append(r["on_text"])
        lines.append("```")
        lines.append("")
        lines.append("---")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {out_path}")
    print(f"directive emission: OFF={off_dir}/{len(results)}  ON={on_dir}/{len(results)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Restore best-effort
        try:
            SETTINGS.write_text("\n".join(read_settings()) + "\n")
        except Exception:
            pass
        print("\ninterrupted")
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
