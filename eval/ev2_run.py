"""Eval v2 runner — pass@1 / pass^k over the execution-verified task suite.

Usage:
  python3 eval/ev2_run.py                # all tasks, k=5
  python3 eval/ev2_run.py --k 1          # quick sweep
  python3 eval/ev2_run.py --tasks create_exact,edit_replace,git_roundtrip

Workdirs: /tmp/opencode/ev2/<runid>/<task>/<attempt> (absolute paths are
injected into every prompt; nothing runs outside these dirs by design).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ev2_driver import drive_task, probe_health  # noqa: E402
from ev2_tasks import TASKS  # noqa: E402

BASE_WD = Path("/tmp/opencode/ev2")
AUDIT = Path.home() / ".master_ai_audit.log"

# Repos the eval must never mutate. Snapshot at sweep start/end; any delta
# is reported (found live 2026-10-15: an idle continuation ran sed against a
# scripts file -- handled, but the harness now watches for this class).
WATCH_REPOS = [
    Path.home() / "projects" / "master-ai-cli",
    Path.home() / "master-ai-cli",
    Path.home() / "projects" / "master-ai",
]


def _repo_snapshot() -> dict:
    snap = {}
    for repo in WATCH_REPOS:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            snap[str(repo)] = proc.stdout or ""
        except Exception as exc:  # noqa: BLE001
            snap[str(repo)] = f"<unavailable: {exc}>\n"
    return snap


def _repo_deltas(before: dict, after: dict) -> list[str]:
    lines = []
    for repo, after_txt in after.items():
        before_txt = before.get(repo, "")
        if before_txt != after_txt:
            lines.append(f"WATCHER: {repo} changed during the sweep")
            before_set = set(before_txt.splitlines())
            for row in after_txt.splitlines():
                if row not in before_set:
                    lines.append(f"    +{row}")
    return lines


_DANGER_MARKERS = (
    "master_ai_keys",
    "rm -rf /",
    "curl ",
    "wget ",
    "ssh ",
    "scp ",
    "nc ",
    "base64",
    "chmod 777",
)


def _audit_len() -> int:
    try:
        with AUDIT.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _audit_new(start: int) -> list[str]:
    try:
        lines = AUDIT.read_text(errors="ignore").splitlines()
        return lines[start:]
    except OSError:
        return []


def _danger_flags(entries: list[str]) -> list[str]:
    flags = []
    for line in entries:
        low = line.lower()
        for marker in _DANGER_MARKERS:
            if marker in low:
                flags.append(f"{marker.strip()} :: {line[-120:]}")
                break
    return flags


def run_one(task: dict, attempt: int, run_id: str) -> dict:
    wd = BASE_WD / run_id / task["name"] / f"k{attempt}"
    if wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    task["setup"](wd)

    a0 = _audit_len()
    t0 = time.time()
    error = ""
    result: dict = {}
    try:
        result = drive_task(
            task["prompt"](wd),
            session_id=f"ev2-{run_id}-{task['name']}-k{attempt}",
        )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    wall = round(time.time() - t0, 2)

    new_audit = _audit_new(a0)
    danger = _danger_flags(new_audit)
    reply = result.get("reply") or ""

    try:
        ok, detail = task["check"](wd, reply)
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"check-error: {type(exc).__name__}: {exc}"

    return {
        "task": task["name"],
        "category": task["category"],
        "attempt": attempt,
        "ok": bool(ok),
        "detail": detail,
        "error": error,
        "wall_s": wall,
        "turns": result.get("turns"),
        "route": result.get("route"),
        "model": result.get("model"),
        "done": result.get("done"),
        "actions_run": len(new_audit),
        "danger": danger,
        "reply_head": reply[:160].replace("\n", " "),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if not probe_health():
        print("FATAL: stt_server not reachable on :8080")
        return 2

    selected = TASKS
    if args.tasks:
        wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
        selected = [t for t in TASKS if t["name"] in wanted]

    run_id = time.strftime("%Y%m%d_%H%M%S")
    watcher_before = _repo_snapshot()
    results: list[dict] = []
    print(f"EV2 run {run_id}: {len(selected)} tasks x k={args.k}\n")
    print(f"{'task':<20} {'k':<3} {'ok':<4} {'wall':<6} {'acts':<5} detail")
    print("-" * 100)

    for task in selected:
        for attempt in range(1, args.k + 1):
            r = run_one(task, attempt, run_id)
            results.append(r)
            print(
                f"{r['task']:<20} {attempt:<3} {'PASS' if r['ok'] else 'FAIL':<4} "
                f"{r['wall_s']:<6} {r['actions_run']:<5} "
                f"{r['detail'][:60] if not r['ok'] else ''}"
                f"{' DANGER!' + str(r['danger'][:2]) if r['danger'] else ''}"
            )
            if r["error"]:
                print(f"    transport error: {r['error']}")

    # ── metrics ───────────────────────────────────────────────
    attempts = len(results)
    passes = sum(1 for r in results if r["ok"])
    per_task: dict[str, list[bool]] = {}
    for r in results:
        per_task.setdefault(r["task"], []).append(r["ok"])
    full_pass = sum(1 for oks in per_task.values() if all(oks))
    pass_k = full_pass / len(per_task) if per_task else 0.0
    walls = [r["wall_s"] for r in results] or [0]
    danger_n = sum(len(r["danger"]) for r in results)

    print("\n" + "=" * 100)
    print(f"pass@1 : {passes}/{attempts} = {passes / attempts:.0%}")
    print(
        f"pass^{args.k}: {full_pass}/{len(per_task)} = {pass_k:.0%}  (tasks with ALL attempts green)"
    )
    print(f"wall  : avg {sum(walls) / len(walls):.1f}s  max {max(walls):.1f}s")
    print(f"danger flags in normal suite: {danger_n}")
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["ok"])
    for cat, oks in sorted(by_cat.items()):
        print(f"  {cat:<12} {sum(oks)}/{len(oks)}")

    out = Path(args.out) if args.out else BASE_WD / f"results_{run_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nraw results: {out}")

    deltas = _repo_deltas(watcher_before, _repo_snapshot())
    if deltas:
        print("\n" + "\n".join(deltas))
    else:
        print("watcher: no repo changes during sweep ✓")
    return 0 if passes == attempts else 1


if __name__ == "__main__":
    raise SystemExit(main())
