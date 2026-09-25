#!/usr/bin/env python3
"""plan_jev_gate — TypeSafe Jev end-gate for Sensei plan mode.

Elijah's design (2026-09-24): before the converged plan-debate plan is shown
for approval, run it through Jev (typesafe/jev, via OpenRouter's decisions
endpoint) as an INDEPENDENT typed validator. The debate models vote on their
own homework; Jev is a second opinion that returns typed probabilities.

Four batched noul judgments over {task, plan}:
  ready     — complete and executable as written
  risky     — destructive beyond the task's stated scope
  concrete  — grounded steps, no generic placeholders
  on_task   — addresses exactly what was asked, no scope creep

Gate:
  pass  = ready >= READY_MIN and risky <= RISKY_MAX
  fail  = one bounded revise round (Jev's failing dimensions fed back as the
          critique) + one re-judge; if still failing, the plan is annotated
          with a flag banner so the operator sees it before approving.

Fail-open always: missing key, network error, API error -> gate reports
indeterminate and the plan flows exactly as before. This gate may flag a plan;
it never blocks the operator from approving one.

Env knobs:
  PLAN_JEV_GATE          on|off      (default on)
  PLAN_JEV_READY_MIN     float       (default 0.60)
  PLAN_JEV_RISKY_MAX     float       (default 0.50)
  PLAN_JEV_RETRY         0|1         (default 1 — one revise+re-judge on fail)

Cost: one 4-question Jev call ≈ $0.00005; warm ≈ 0.3-2s. Retry adds one
merger-model revise call + one more Jev call.

CLI (self-test):
  python3 plan_jev_gate.py --task "fix the flaky test" --plan plan.txt
  python3 plan_jev_gate.py --selftest          # runs the stubbed test suite
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import urllib.error
import urllib.request
from typing import Any

# ── Jev via OpenRouter decisions endpoint (no /v1 in the path — verified) ──
JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "~typesafe/jev-latest"
_HERMES_ENV = pathlib.Path.home() / ".hermes" / ".env"

QUESTIONS: dict[str, dict] = {
    "ready": {
        "type": "noul",
        "instructions": (
            "Does this plan read as complete and ready to execute as written — "
            "concrete steps with no missing pieces that would force a human to "
            "guess what to do?"
        ),
    },
    "risky": {
        "type": "noul",
        "instructions": (
            "Does this plan include any step that is destructive or hard to "
            "reverse BEYOND what the task itself asks for — e.g. deleting user "
            "data, wiping directories outside the task's target, killing system "
            "services it does not own, or force-pushing git history? Steps that "
            "only affect the task's own build artifacts or temp files do NOT "
            "count as risky."
        ),
    },
    "concrete": {
        "type": "noul",
        "instructions": (
            "Are the plan's steps concrete and grounded (real files, paths, "
            "commands, service names) rather than generic placeholders like "
            "'the config file' or 'set things up'?"
        ),
    },
    "on_task": {
        "type": "noul",
        "instructions": (
            "Does this plan address exactly what the task asks — no significant "
            "scope creep and no ignored part of the request?"
        ),
    },
}

_DIMENSIONS = ("ready", "risky", "concrete", "on_task")


# ── env / key helpers ────────────────────────────────────────────────────
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "on", "true", "yes")


def jev_api_key() -> str | None:
    """OPENROUTER_API_KEY from env, else ~/.hermes/.env (export-prefixed OK)."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        if _HERMES_ENV.exists():
            for line in _HERMES_ENV.read_text().splitlines():
                m = re.match(
                    r"^(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.+)$", line.strip()
                )
                if m:
                    val = m.group(1).strip().strip('"').strip("'")
                    if val:
                        return val
    except Exception:
        pass
    return None


# ── HTTP (isolated for test stubbing) ────────────────────────────────────
def _http_decisions(payload: dict, api_key: str, timeout: int = 45) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        JEV_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ask_jev_nouls(
    state: dict, names: tuple[str, ...] = _DIMENSIONS, api_key: str | None = None
) -> dict | None:
    """One batched Jev call. Returns {name: prob} or None on any failure."""
    key = api_key or jev_api_key()
    if not key:
        return None
    questions = {n: QUESTIONS[n] for n in names if n in QUESTIONS}
    payload = {"model": JEV_MODEL, "state": state, "questions": questions}
    try:
        resp = _http_decisions(payload, key)
        answers = resp.get("answers", {})
        out: dict[str, float] = {}
        for n in questions:
            a = answers.get(n) or {}
            p = a.get("noul")
            if isinstance(p, (int, float)) and 0.0 <= p <= 1.0:
                out[n] = float(p)
        # Require every requested dimension back; partial answers = unusable.
        if len(out) != len(questions):
            return None
        return out
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


# ── gate logic ───────────────────────────────────────────────────────────
def _fail_dims(probs: dict, ready_min: float, risky_max: float) -> list[str]:
    fails = []
    if probs.get("ready", 1.0) < ready_min:
        fails.append(f"ready={probs.get('ready', 0):.2f}<{ready_min}")
    if probs.get("risky", 0.0) > risky_max:
        fails.append(f"risky={probs.get('risky', 1):.2f}>{risky_max}")
    # Advisory only (logged, never gate-blocking): concrete / on_task.
    return fails


def _annotate(plan: str, banner_lines: list[str]) -> str:
    if not banner_lines:
        return plan
    return "\n".join(banner_lines).rstrip() + "\n\n" + plan


def _revise_prompt(task: str, plan: str, failing: list[str]) -> str:
    dims = ", ".join(failing)
    return (
        f"Task:\n{task}\n\nPlan:\n{plan}\n\n"
        f"An independent reviewer flagged this plan on: {dims}. "
        f"Revise the plan to fix ONLY what the flag implies "
        f"({'make it complete and directly on-task' if 'ready' in dims or 'on_task' in dims else 'reduce dangerous steps; keep the task achievable'}). "
        f"Output only the full revised plan."
    )


def run_jev_gate(task: str, plan: str, revise_fn=None) -> dict:
    """Entry point. Never raises; never blocks. Returns a report dict:

    { 'passed': True|False|None (None = indeterminate/fail-open),
      'gate': 'pass'|'flagged'|'skipped',
      'ready','risky','concrete','on_task': float|None,
      'revised': bool,
      'annotated_plan': str,   # plan, possibly with banner prepended
      'progress': [str, ...],  # human-readable lines for the TUI
    }
    """
    report: dict[str, Any] = {
        "passed": None,
        "gate": "skipped",
        "revised": False,
        "ready": None,
        "risky": None,
        "concrete": None,
        "on_task": None,
        "annotated_plan": plan,
        "progress": [],
    }
    progress = report["progress"]

    if not (plan or "").strip():
        return report
    if os.environ.get("PLAN_JEV_GATE", "on").strip().lower() in ("0", "off", "no"):
        progress.append("[jev gate] disabled (PLAN_JEV_GATE=off)")
        return report
    if not jev_api_key():
        progress.append("[jev gate] skipped — no OPENROUTER_API_KEY")
        return report

    state = {"task": (task or "")[:8000], "plan": (plan or "")[:24000]}
    probs = ask_jev_nouls(state)
    if probs is None:
        progress.append("[jev gate] skipped — Jev call failed (fail-open)")
        return report
    for k in _DIMENSIONS:
        report[k] = probs.get(k)
    ready_min = _env_float("PLAN_JEV_READY_MIN", 0.60)
    risky_max = _env_float("PLAN_JEV_RISKY_MAX", 0.50)

    failing = _fail_dims(probs, ready_min, risky_max)
    if not failing:
        report["passed"] = True
        report["gate"] = "pass"
        progress.append(
            "[jev gate] PASS  ready={ready:.2f} risky={risky:.2f} "
            "concrete={concrete:.2f} on_task={on_task:.2f}".format(
                ready=probs["ready"],
                risky=probs["risky"],
                concrete=probs["concrete"],
                on_task=probs["on_task"],
            )
        )
        return report

    report["passed"] = False
    report["gate"] = "flagged"
    progress.append(
        "[jev gate] flag: {}  (ready={:.2f} risky={:.2f} "
        "concrete={:.2f} on_task={:.2f})".format(
            ", ".join(failing),
            probs["ready"],
            probs["risky"],
            probs["concrete"],
            probs["on_task"],
        )
    )

    # One bounded revise round: feed the failing dimensions back as critique,
    # re-judge. Never loops (max 1 retry), never blocks.
    if _env_bool("PLAN_JEV_RETRY", True) and callable(revise_fn):
        try:
            revised = (revise_fn(_revise_prompt(task, plan, failing)) or "").strip()
        except Exception as e:  # revise model failure -> keep flagged original
            progress.append(f"[jev gate] revise call failed: {e}")
            revised = ""
        if revised:
            rprobs = ask_jev_nouls({"task": state["task"], "plan": revised[:24000]})
            if rprobs is not None:
                rfailing = _fail_dims(rprobs, ready_min, risky_max)
                for k in _DIMENSIONS:
                    report[k] = rprobs.get(k, report[k])
                if not rfailing:
                    report["passed"] = True
                    report["gate"] = "pass"
                    report["revised"] = True
                    report["annotated_plan"] = _annotate(
                        revised,
                        [
                            f"> [jev gate] plan revised once after flag "
                            f"({', '.join(failing)}) — now "
                            f"ready={rprobs['ready']:.2f} risky={rprobs['risky']:.2f} "
                            f"concrete={rprobs['concrete']:.2f} "
                            f"on_task={rprobs['on_task']:.2f}"
                        ],
                    )
                    progress.append(
                        "[jev gate] PASS after revise  ready={:.2f} "
                        "risky={:.2f}".format(rprobs["ready"], rprobs["risky"])
                    )
                    return report
                report["gate"] = "flagged"
                progress.append(
                    "[jev gate] still flagged after revise: " + ", ".join(rfailing)
                )
                report["annotated_plan"] = _annotate(
                    revised,
                    [
                        "⚠ [jev gate] FLAGGED — independent review scored this plan:",
                        f"> {', '.join(rfailing)}  (ready={rprobs['ready']:.2f} "
                        f"risky={rprobs['risky']:.2f} "
                        f"concrete={rprobs['concrete']:.2f} "
                        f"on_task={rprobs['on_task']:.2f})",
                        "> Read it carefully before approving.",
                    ],
                )
                return report
            # re-judge failed (network etc.) — flag the revised text as-is
            report["revised"] = True
            report["annotated_plan"] = _annotate(
                revised,
                [
                    "⚠ [jev gate] FLAGGED — one revise round ran but re-check "
                    "could not complete (fail-open). Review before approving."
                ],
            )
            return report

    report["annotated_plan"] = _annotate(
        plan,
        [
            "⚠ [jev gate] FLAGGED — independent review scored this plan:",
            f"> {', '.join(failing)}  (ready={probs['ready']:.2f} "
            f"risky={probs['risky']:.2f} concrete={probs['concrete']:.2f} "
            f"on_task={probs['on_task']:.2f})",
            "> Read it carefully before approving.",
        ],
    )
    return report


# ── CLI ──────────────────────────────────────────────────────────────────
def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Jev end-gate for Sensei plans")
    ap.add_argument("--task", required=True)
    ap.add_argument("--plan", help="path to plan text file (default: stdin)")
    ap.add_argument("--no-retry", action="store_true")
    args = ap.parse_args()

    import sys

    plan_text: str = (
        pathlib.Path(args.plan).read_text() if args.plan else sys.stdin.read()
    )
    out = run_jev_gate(
        args.task,
        plan_text,
        revise_fn=None if args.no_retry else (lambda prompt: _import_revise()(prompt)),
    )
    for line in out["progress"]:
        print(line)
    print()
    print(out["annotated_plan"])
    return 0


def _import_revise():
    """CLI revise lane: use the reasoning loop's model router if available."""
    try:
        import master_ai
        from sensei_reasoning_loop import _model_chat

        model = (
            getattr(master_ai, "PINNED_MODEL", None)
            or master_ai.MODELS.get("master")
            or "master-ai"
        )
        return lambda prompt: _model_chat(model, "", prompt, num_predict=2000)[0]
    except Exception:
        return lambda prompt: ""


# ── selftest (stubbed HTTP — no network) ─────────────────────────────────
def _selftest() -> int:
    import plan_jev_gate as g

    def stub(probs_by_call):
        calls = {"n": 0}

        def _fake(payload, key, timeout=45):
            probs = probs_by_call[min(calls["n"], len(probs_by_call) - 1)]
            calls["n"] += 1
            return {"answers": {n: {"noul": p} for n, p in probs.items()}}

        return _fake

    fails = []
    checks = {"n": 0}

    def check(name, cond):
        checks["n"] += 1
        if cond:
            print(f"  PASS {name}")
        else:
            fails.append(name)
            print(f"  FAIL {name}")

    # 1. clean pass, no annotation
    g._http_decisions = stub(
        [{"ready": 0.92, "risky": 0.08, "concrete": 0.95, "on_task": 0.97}]
    )
    out = g.run_jev_gate("t", "step 1\nstep 2", revise_fn=lambda p: "x")
    check(
        "pass-case",
        out["passed"] is True
        and out["gate"] == "pass"
        and out["annotated_plan"] == "step 1\nstep 2",
    )

    # 2. flag -> revise -> re-judge passes
    g._http_decisions = stub(
        [
            {"ready": 0.41, "risky": 0.10, "concrete": 0.80, "on_task": 0.85},
            {"ready": 0.85, "risky": 0.05, "concrete": 0.90, "on_task": 0.92},
        ]
    )
    out = g.run_jev_gate("t", "vague plan", revise_fn=lambda p: "better plan")
    check(
        "revise-recover",
        out["passed"] is True
        and out["revised"] is True
        and out["annotated_plan"].startswith("> [jev gate]")
        and "better plan" in out["annotated_plan"],
    )

    # 3. flag -> revise -> still flagged (banner + revised text)
    g._http_decisions = stub(
        [
            {"ready": 0.30, "risky": 0.10, "concrete": 0.5, "on_task": 0.5},
            {"ready": 0.45, "risky": 0.70, "concrete": 0.5, "on_task": 0.5},
        ]
    )
    out = g.run_jev_gate("t", "bad plan", revise_fn=lambda p: "still bad")
    check(
        "still-flagged",
        out["passed"] is False
        and out["gate"] == "flagged"
        and out["annotated_plan"].startswith("⚠ [jev gate] FLAGGED")
        and "still bad" in out["annotated_plan"],
    )

    # 4. flag, no revise fn -> flag original
    g._http_decisions = stub(
        [{"ready": 0.30, "risky": 0.10, "concrete": 0.5, "on_task": 0.5}]
    )
    out = g.run_jev_gate("t", "vague plan", revise_fn=None)
    check(
        "no-retry",
        out["passed"] is False
        and out["revised"] is False
        and out["annotated_plan"].startswith("⚠ [jev gate] FLAGGED"),
    )

    # 5. missing key -> fail-open skip
    real_key = g.jev_api_key
    g.jev_api_key = lambda: None
    out = g.run_jev_gate("t", "plan", revise_fn=None)
    g.jev_api_key = real_key
    check("fail-open-key", out["passed"] is None and out["gate"] == "skipped")

    # 6. network failure -> fail-open
    def _boom(payload, key, timeout=45):
        raise urllib.error.URLError("no net")

    g._http_decisions = _boom
    g.jev_api_key = lambda: "k"
    out = g.run_jev_gate("t", "plan", revise_fn=None)
    check("fail-open-net", out["passed"] is None and out["gate"] == "skipped")

    # 7. env kill-switch
    os.environ["PLAN_JEV_GATE"] = "off"
    out = g.run_jev_gate("t", "plan", revise_fn=None)
    os.environ.pop("PLAN_JEV_GATE", None)
    check("kill-switch", out["gate"] == "skipped" and "disabled" in out["progress"][0])

    # 8. threshold env overrides honored
    g._http_decisions = stub(
        [{"ready": 0.70, "risky": 0.30, "concrete": 0.5, "on_task": 0.5}]
    )
    out = g.run_jev_gate("t", "plan", revise_fn=None)
    check("default-thresholds-pass", out["passed"] is True)
    os.environ["PLAN_JEV_READY_MIN"] = "0.80"
    out = g.run_jev_gate("t", "plan", revise_fn=None)
    os.environ.pop("PLAN_JEV_READY_MIN", None)
    check("ready-min-override", out["passed"] is False)

    # 9. payload shape (model + noul question types)
    seen = {}

    def _capture(payload, key, timeout=45):
        seen.update(payload)
        return {"answers": {n: {"noul": 0.9} for n in payload["questions"]}}

    g._http_decisions = _capture
    g.run_jev_gate("t", "plan", revise_fn=None)
    check("payload-model", seen.get("model") == g.JEV_MODEL)
    check(
        "payload-noul",
        all(q.get("type") == "noul" for q in seen.get("questions", {}).values())
        and len(seen.get("questions", {})) == 4,
    )

    print()
    print(
        f"  {checks['n']} checks — {'ALL OK' if not fails else str(len(fails)) + ' FAILURES'}"
    )
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) > 1 and _sys.argv[1] == "--selftest":
        _sys.exit(_selftest())
    _sys.exit(_main())
