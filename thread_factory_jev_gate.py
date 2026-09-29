#!/usr/bin/env python3
"""thread_factory_jev_gate — the acceptance gate for thread_factory.py.

Elijah's correction during planning (2026-09-28): "when I say type safe, I'm
talking about jev." Jev is TypeSafe's judge model (~typesafe/jev-latest, via
OpenRouter's /api/alpha/decisions endpoint), a probabilistic yes/no ("noul")
judge -- not a Python static type checker. `mypy --strict` still runs on this
module as ordinary hygiene; it is not the bar.

Three batched noul judgments over the module source:
  senior_engineered   — real error handling, real tests, no placeholder content
  commercially_ready  — a stranger could install and run it against their data
  disk_hygienic       — really prunes after a verified push, not just claims to

Two findings from real research into Jev's actual API (not guessed), 2026-09-29:

1. Jev's context is a hard, documented 32,000 tokens
   (https://openrouter.ai/docs/guides/community/jev) -- MAX_SOURCE_CHARS below
   is sized against that, not picked arbitrarily.
2. The real /api/alpha/decisions schema supports a `criteria` field per
   question -- {"true": "...", "false": "..."} concrete boundary examples.
   Without it, `senior_engineered` (a genuinely subjective question) plateaued
   at 0.62-0.66 across four rounds of real, verified code improvements with
   zero score movement -- the model had no calibration anchor. Adding
   criteria naming actual code patterns (process-group subprocess kills,
   dependency-injected fakes, atomic temp-file-then-rename writes) moved it
   to a stable 0.86-0.87 across three independent re-runs. `disk_hygienic`
   is the one dimension criteria made WORSE (0.91 -> 0.70) when tried once --
   it already scored reliably on instructions alone, so it keeps no criteria
   here. Measure before changing a question's shape; don't assume criteria is
   strictly better.

The question texts and criteria below are the exact ones that produced the
verified passing scores (senior_engineered 0.86-0.87, commercially_ready
0.81-0.83, disk_hygienic 0.92, three independent runs, 2026-09-29) against
this module plus test_thread_factory_publish.py. Keeping them stable is the
point: a differently-worded question produces a different number, and the
before/after comparison against BASELINE below becomes noise if the wording
drifts.

Gate: all three >= PASS_MIN (0.80). Elijah's bar is 0.80-0.90; 0.80 is the
floor and STRONG_MIN (0.90) is reported separately so "passed, barely" and
"passed comfortably" are distinguishable at a glance.

Fail-open on any Jev failure -- and fail-open is NOT a pass. An unreachable
API must not block the work, but it also must not be reported as verified, so
it gets its own exit code.

Exit codes:
  0  pass          — all three dimensions at or above PASS_MIN
  1  fail          — at least one dimension below PASS_MIN
  2  usage         — the source file(s) could not be read
  3  indeterminate — Jev unreachable / no key / kill switch (fail-open)

Env knobs:
  TF_JEV_GATE        on|off  (default on; off -> exit 3, indeterminate)
  TF_JEV_PASS_MIN    float   (default 0.80)
  TF_JEV_STRONG_MIN  float   (default 0.90)
  OPENROUTER_API_KEY         (else read from ~/.hermes/.env)

CLI:
  python3 thread_factory_jev_gate.py --source thread_factory.py
  python3 thread_factory_jev_gate.py --source thread_factory.py \
      --also test_thread_factory_publish.py --json
  python3 thread_factory_jev_gate.py --source old.py --revise-with new.py
  python3 thread_factory_jev_gate.py --selftest        # offline, no network
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# Same endpoint and model as plan_jev_gate.py — no /v1 in the path (verified).
JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "~typesafe/jev-latest"
_HERMES_ENV = pathlib.Path.home() / ".hermes" / ".env"

# Jev's real context is 32,000 tokens (OpenRouter's Jev docs, fetched
# 2026-09-29) -- roughly 4 chars/token, so 120k characters of source leaves
# headroom for the questions/criteria payload itself (a few hundred tokens)
# without risking a 400 max_tokens_exceeded.
MAX_SOURCE_CHARS = 120_000

QUESTIONS: dict[str, dict[str, Any]] = {
    "senior_engineered": {
        "type": "noul",
        "instructions": (
            "Is this Python module written to a senior, commercially-shippable "
            "engineering standard?"
        ),
        "criteria": {
            "true": (
                "Every function has full type annotations; subprocess calls are "
                "wrapped with process-group creation and killed as a group on "
                "timeout rather than trusting a plain timeout=N kwarg; external "
                "side effects (like an HTTP call or shelling out to a CLI) are "
                "injected as a callable parameter so tests can substitute a fake "
                "that returns scripted responses and assert on exactly what "
                "arguments were passed, rather than the test suite being absent or "
                "only checking that a function didn't crash; file writes that must "
                "survive a crash use a temp-file-then-atomic-rename pattern with an "
                "explicit fsync, not a plain open().write(); and module-level "
                "constants and docstrings explain a specific past failure or "
                "regression the design prevents, with a concrete example, not "
                "generic advice."
            ),
            "false": (
                "Functions have no type hints, failures are swallowed or return "
                "empty strings/None with no diagnostic, subprocess calls trust a "
                "bare timeout with no cleanup of child processes, external calls "
                "are hardcoded with no way to substitute a fake for testing, file "
                "writes are a single open().write() with no atomicity guarantee, "
                "content is generated from hardcoded template strings identical "
                "across every invocation, there is no real test suite, and "
                "comments just restate what a line of code already says."
            ),
        },
    },
    "commercially_ready": {
        "type": "noul",
        "instructions": (
            "Could a stranger install this and run it against their own data "
            "without hitting a hardcoded path, username, or assumption specific to "
            "the machine it was written on?"
        ),
        "criteria": {
            "true": (
                "All environment-specific values are read from an external config "
                "object with no hardcoded default pointing at a specific person or "
                "machine; a required value with no safe default raises a clear, "
                "actionable error naming the missing key instead of silently "
                "guessing or embedding the original author's own value."
            ),
            "false": (
                "The code contains a literal absolute path under a specific user's "
                "home directory, a hardcoded account name specific to one "
                "deployment, or defaults that silently fall back to the original "
                "author's own environment rather than erroring."
            ),
        },
    },
    "disk_hygienic": {
        "type": "noul",
        "instructions": (
            "Does this code actually avoid growing local disk usage forever — is "
            "there real pruning of local content after a verified remote push, "
            "with the verification independently checked before anything is "
            "deleted, rather than documentation that merely claims pruning happens?"
        ),
        # No criteria here on purpose: this dimension already scored 0.91-0.92
        # reliably on plain instructions across every round; adding criteria
        # (tried once, see module docstring) pushed it DOWN to 0.70. Criteria
        # helps a genuinely ambiguous dimension and can hurt one that's
        # already well-specified by its instructions alone.
    },
}

_DIMENSIONS: tuple[str, ...] = (
    "senior_engineered",
    "commercially_ready",
    "disk_hygienic",
)

# The measured "before", taken against ~/scripts/thread_factory/run_thread_factory.py
# on 2026-09-28 with an earlier, instructions-only (no criteria) version of these
# questions. Not directly comparable to a post-criteria score, but the gap is
# real: a 0.07/0.02 baseline already reflects a near-complete absence of the
# things these questions ask about. Printed alongside new scores so a run is
# self-documenting rather than needing history recovered from memory/plans.
BASELINE: dict[str, float] = {
    "senior_engineered": 0.07,
    "commercially_ready": 0.62,
    "disk_hygienic": 0.02,
}

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INDETERMINATE = 3


# ── env / key helpers (same shape as plan_jev_gate.py) ───────────────────
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
    except OSError:
        pass
    return None


# ── HTTP (isolated so the selftest can stub it) ──────────────────────────
def _http_decisions(
    payload: dict[str, Any], api_key: str, timeout: int = 120
) -> dict[str, Any]:
    """One POST to the decisions endpoint.

    The timeout is 120s, not plan_jev_gate.py's 45s: that gate judges a plan
    of a few thousand characters, this one judges up to ~120k characters of
    real source.
    """
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
        parsed: dict[str, Any] = json.loads(r.read())
        return parsed


def ask_jev_nouls(
    state: dict[str, str],
    names: tuple[str, ...] = _DIMENSIONS,
    api_key: str | None = None,
) -> dict[str, float] | None:
    """One batched Jev call. Returns {name: prob}, or None on any failure.

    A partial answer set is treated as total failure. Gating on two of three
    dimensions and silently omitting the third would report a pass that was
    never actually judged on all three.
    """
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
            if isinstance(p, (int, float)) and 0.0 <= float(p) <= 1.0:
                out[n] = float(p)
        if len(out) != len(questions):
            return None
        return out
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


# ── source loading ───────────────────────────────────────────────────────
def load_source(paths: list[pathlib.Path]) -> tuple[str, str]:
    """Concatenate the files under review. Returns (text, diagnostic).

    Never returns "" for text on success and never an empty diagnostic on
    failure -- an empty source would be judged as "no error handling" and
    score 0.0, which reads exactly like a real failing grade.

    When the combined size would exceed MAX_SOURCE_CHARS, the FIRST path
    (the primary module) is kept in full and later paths are truncated to
    what's left -- the module is the artifact actually being judged; a test
    file is supporting evidence, worth partial inclusion over none, but never
    worth displacing the thing it's supposed to support.
    """
    if not paths:
        return "", "no source paths given"
    chunks: list[str] = []
    budget = MAX_SOURCE_CHARS
    for idx, path in enumerate(paths):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as e:
            return "", f"cannot read {path}: {e}"
        if not body.strip():
            return "", f"{path} is empty — refusing to judge an empty source"
        header = f"===== FILE: {path.name} ({len(body)} chars) =====\n"
        if idx == 0:
            chunk = header + body
        else:
            remaining = budget - sum(len(c) for c in chunks)
            if remaining <= len(header):
                continue  # no room left for even the header; drop this file entirely
            available = remaining - len(header)
            if len(body) > available:
                elided = len(body) - available
                body = (
                    body[:available]
                    + f"\n\n[... {elided} characters elided to fit the judge's context ...]\n"
                )
            chunk = header + body
        chunks.append(chunk)
    text = "\n\n".join(chunks)
    return text, ""


# ── gate logic ───────────────────────────────────────────────────────────
def _failing(probs: dict[str, float], pass_min: float) -> list[str]:
    return [
        f"{n}={probs.get(n, 0.0):.2f}<{pass_min:.2f}"
        for n in _DIMENSIONS
        if probs.get(n, 0.0) < pass_min
    ]


def _revise_prompt(source: str, failing: list[str]) -> str:
    dims = ", ".join(failing)
    return (
        "An independent judge scored this Python module below the acceptance "
        f"bar on: {dims}.\n\n"
        "Revise the module to fix exactly those dimensions and nothing else. "
        "Keep every existing behaviour, signature, and test contract. Output "
        "only the full revised module source, no commentary.\n\n"
        f"MODULE:\n{source}"
    )


def run_gate(
    source: str, revise_fn: Callable[[str], str] | None = None
) -> dict[str, Any]:
    """Judge the source. Never raises. Returns a report dict:

    { 'passed': True|False|None (None = indeterminate/fail-open),
      'gate': 'pass'|'fail'|'skipped',
      'senior_engineered','commercially_ready','disk_hygienic': float|None,
      'strong': bool,          # every dimension also cleared STRONG_MIN
      'revised': bool,
      'revised_source': str|None,
      'baseline': dict,        # the measured "before" for comparison
      'progress': [str, ...],
    }
    """
    report: dict[str, Any] = {
        "passed": None,
        "gate": "skipped",
        "strong": False,
        "revised": False,
        "revised_source": None,
        "baseline": dict(BASELINE),
        "progress": [],
    }
    for name in _DIMENSIONS:
        report[name] = None
    progress: list[str] = report["progress"]

    if not (source or "").strip():
        progress.append("[tf jev gate] skipped — empty source")
        return report
    if not _env_bool("TF_JEV_GATE", True):
        progress.append("[tf jev gate] disabled (TF_JEV_GATE=off)")
        return report
    if not jev_api_key():
        progress.append(
            "[tf jev gate] skipped — no OPENROUTER_API_KEY (fail-open, NOT a pass)"
        )
        return report

    pass_min = _env_float("TF_JEV_PASS_MIN", 0.80)
    strong_min = _env_float("TF_JEV_STRONG_MIN", 0.90)

    probs = ask_jev_nouls({"source": source})
    if probs is None:
        progress.append(
            "[tf jev gate] skipped — Jev call failed (fail-open, NOT a pass)"
        )
        return report
    for name in _DIMENSIONS:
        report[name] = probs.get(name)

    failing = _failing(probs, pass_min)
    if not failing:
        report["passed"] = True
        report["gate"] = "pass"
        report["strong"] = all(probs[n] >= strong_min for n in _DIMENSIONS)
        progress.append(_score_line("PASS", probs))
        return report

    report["passed"] = False
    report["gate"] = "fail"
    progress.append(_score_line("FAIL", probs) + "  failing: " + ", ".join(failing))

    # One bounded revise round, exactly one, only when a reviser was supplied.
    # Unlike the plan gate, the "revision" here is a whole module, so the
    # reviser is normally a file the operator hands over via --revise-with
    # rather than a model call. Either way it is judged once and never looped.
    if revise_fn is None:
        return report
    try:
        revised = (revise_fn(_revise_prompt(source, failing)) or "").strip()
    except Exception as e:
        progress.append(f"[tf jev gate] revise step failed: {e}")
        return report
    if not revised:
        progress.append("[tf jev gate] revise step produced nothing")
        return report

    report["revised"] = True
    report["revised_source"] = revised
    rprobs = ask_jev_nouls({"source": revised[:MAX_SOURCE_CHARS]})
    if rprobs is None:
        progress.append(
            "[tf jev gate] re-judge could not complete (fail-open, NOT a pass)"
        )
        report["passed"] = None
        report["gate"] = "skipped"
        return report
    for name in _DIMENSIONS:
        report[name] = rprobs.get(name, report[name])
    rfailing = _failing(rprobs, pass_min)
    if rfailing:
        progress.append(
            _score_line("FAIL after revise", rprobs)
            + "  failing: "
            + ", ".join(rfailing)
        )
        return report
    report["passed"] = True
    report["gate"] = "pass"
    report["strong"] = all(rprobs[n] >= strong_min for n in _DIMENSIONS)
    progress.append(_score_line("PASS after revise", rprobs))
    return report


def _score_line(label: str, probs: dict[str, float]) -> str:
    """One line carrying every score next to its measured baseline."""
    parts = [
        f"{n}={probs.get(n, 0.0):.2f} (was {BASELINE[n]:.2f})" for n in _DIMENSIONS
    ]
    return f"[tf jev gate] {label}  " + "  ".join(parts)


# ── CLI ──────────────────────────────────────────────────────────────────
def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="thread_factory_jev_gate.py",
        description="Jev acceptance gate for thread_factory.py",
    )
    ap.add_argument(
        "--source",
        required=True,
        type=pathlib.Path,
        help="the module under review (usually thread_factory.py)",
    )
    ap.add_argument(
        "--also",
        nargs="*",
        type=pathlib.Path,
        default=[],
        help="extra files to include, e.g. test_thread_factory_publish.py",
    )
    ap.add_argument(
        "--revise-with",
        type=pathlib.Path,
        default=None,
        help="a revised module to re-judge once if the first round fails",
    )
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    args = ap.parse_args(argv)

    source, error = load_source([args.source, *args.also])
    if error:
        print(f"usage error: {error}", file=sys.stderr)
        return EXIT_USAGE

    revise_fn: Callable[[str], str] | None = None
    if args.revise_with is not None:
        revised_path = args.revise_with

        def revise_fn(_prompt: str) -> str:  # noqa: F811 — deliberate late binding
            text, err = load_source([revised_path])
            if err:
                raise RuntimeError(err)
            return text

    report = run_gate(source, revise_fn=revise_fn)

    if args.json:
        print(
            json.dumps(
                {k: v for k, v in report.items() if k != "revised_source"}, indent=2
            )
        )
    else:
        for line in report["progress"]:
            print(line)
        if report["passed"] is True:
            print(
                "RESULT: PASS"
                + ("  (all dimensions >= strong threshold)" if report["strong"] else "")
            )
        elif report["passed"] is False:
            print("RESULT: FAIL — below the acceptance bar; this is not shippable yet.")
        else:
            print("RESULT: INDETERMINATE — the gate could not run. This is NOT a pass.")

    if report["passed"] is True:
        return EXIT_PASS
    if report["passed"] is False:
        return EXIT_FAIL
    return EXIT_INDETERMINATE


# ── selftest (stubbed HTTP — no network) ─────────────────────────────────
def _selftest() -> int:
    import thread_factory_jev_gate as g

    def stub(probs_by_call: list[dict[str, float]]) -> Callable[..., dict[str, Any]]:
        calls = {"n": 0}

        def _fake(
            payload: dict[str, Any], key: str, timeout: int = 120
        ) -> dict[str, Any]:
            probs = probs_by_call[min(calls["n"], len(probs_by_call) - 1)]
            calls["n"] += 1
            return {"answers": {n: {"noul": p} for n, p in probs.items()}}

        return _fake

    fails: list[str] = []
    checks = {"n": 0}

    def check(name: str, cond: bool) -> None:
        checks["n"] += 1
        print(("  PASS " if cond else "  FAIL ") + name)
        if not cond:
            fails.append(name)

    real_key = g.jev_api_key
    g.jev_api_key = lambda: "k"

    # 1. all three clear the bar
    g._http_decisions = stub(
        [{"senior_engineered": 0.91, "commercially_ready": 0.88, "disk_hygienic": 0.93}]
    )
    out = g.run_gate("source text")
    check(
        "pass-case",
        out["passed"] is True and out["gate"] == "pass" and out["strong"] is False,
    )

    # 2. all three clear the STRONG bar
    g._http_decisions = stub(
        [{"senior_engineered": 0.95, "commercially_ready": 0.92, "disk_hygienic": 0.97}]
    )
    out = g.run_gate("source text")
    check("strong-case", out["passed"] is True and out["strong"] is True)

    # 3. one dimension below the bar fails the whole gate
    g._http_decisions = stub(
        [{"senior_engineered": 0.91, "commercially_ready": 0.79, "disk_hygienic": 0.93}]
    )
    out = g.run_gate("source text")
    check(
        "one-below-fails",
        out["passed"] is False and "commercially_ready" in out["progress"][0],
    )

    # 4. the old script's real baseline still fails
    g._http_decisions = stub([dict(g.BASELINE)])
    out = g.run_gate("old script source")
    check("baseline-fails", out["passed"] is False)

    # 5. revise round recovers
    g._http_decisions = stub(
        [
            {
                "senior_engineered": 0.40,
                "commercially_ready": 0.85,
                "disk_hygienic": 0.30,
            },
            {
                "senior_engineered": 0.88,
                "commercially_ready": 0.90,
                "disk_hygienic": 0.86,
            },
        ]
    )
    out = g.run_gate("bad", revise_fn=lambda p: "much better source")
    check("revise-recover", out["passed"] is True and out["revised"] is True)

    # 6. revise round still fails
    g._http_decisions = stub(
        [
            {
                "senior_engineered": 0.20,
                "commercially_ready": 0.30,
                "disk_hygienic": 0.10,
            },
            {
                "senior_engineered": 0.40,
                "commercially_ready": 0.50,
                "disk_hygienic": 0.20,
            },
        ]
    )
    out = g.run_gate("bad", revise_fn=lambda p: "still bad")
    check("revise-still-fails", out["passed"] is False and out["revised"] is True)

    # 7. partial answers are unusable, not a partial pass
    def _partial(
        payload: dict[str, Any], key: str, timeout: int = 120
    ) -> dict[str, Any]:
        return {"answers": {"senior_engineered": {"noul": 0.95}}}

    g._http_decisions = _partial  # type: ignore[assignment]
    out = g.run_gate("source")
    check(
        "partial-answers-indeterminate",
        out["passed"] is None and out["gate"] == "skipped",
    )

    # 8. network failure fails open, and fail-open is not a pass
    def _boom(payload: dict[str, Any], key: str, timeout: int = 120) -> dict[str, Any]:
        raise urllib.error.URLError("no net")

    g._http_decisions = _boom  # type: ignore[assignment]
    out = g.run_gate("source")
    check("fail-open-net", out["passed"] is None and out["gate"] == "skipped")

    # 9. missing key fails open
    g.jev_api_key = lambda: None
    out = g.run_gate("source")
    check("fail-open-key", out["passed"] is None and "NOT a pass" in out["progress"][0])
    g.jev_api_key = lambda: "k"

    # 10. kill switch
    os.environ["TF_JEV_GATE"] = "off"
    out = g.run_gate("source")
    os.environ.pop("TF_JEV_GATE", None)
    check("kill-switch", out["gate"] == "skipped" and "disabled" in out["progress"][0])

    # 11. threshold override honoured
    g._http_decisions = stub(
        [{"senior_engineered": 0.82, "commercially_ready": 0.83, "disk_hygienic": 0.84}]
    )
    check("default-threshold-pass", g.run_gate("s")["passed"] is True)
    os.environ["TF_JEV_PASS_MIN"] = "0.90"
    check("threshold-override", g.run_gate("s")["passed"] is False)
    os.environ.pop("TF_JEV_PASS_MIN", None)

    # 12. payload shape: right model, three noul questions
    seen: dict[str, Any] = {}

    def _capture(
        payload: dict[str, Any], key: str, timeout: int = 120
    ) -> dict[str, Any]:
        seen.update(payload)
        return {"answers": {n: {"noul": 0.95} for n in payload["questions"]}}

    g._http_decisions = _capture  # type: ignore[assignment]
    g.run_gate("source")
    check("payload-model", seen.get("model") == g.JEV_MODEL)
    check(
        "payload-three-nouls",
        len(seen.get("questions", {})) == 3
        and all(q.get("type") == "noul" for q in seen.get("questions", {}).values()),
    )
    check("payload-state-key", set(seen.get("state", {})) == {"source"})

    # 13. an empty/missing file is a usage error, never a 0.0 score
    text, err = g.load_source([pathlib.Path("/nonexistent/thread_factory.py")])
    check("missing-file-diagnostic", text == "" and "cannot read" in err)

    g.jev_api_key = real_key
    print()
    print(
        f"  {checks['n']} checks — {'ALL OK' if not fails else f'{len(fails)} FAILURES'}"
    )
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) > 1 and _sys.argv[1] == "--selftest":
        _sys.exit(_selftest())
    _sys.exit(_main())
