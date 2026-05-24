"""
run_apply_skill.py — Outer loop connecting skill_runtime ↔ browser_bridge.

What this does:
  1. Starts (or resumes) an apply-job-session skill run
  2. When the skill INTERRUPTs for browser directives, executes them via
     browser_bridge, feeds results back into state, and resumes
  3. When the skill INTERRUPTs for OPERATOR REVIEW (submit gate or v1 scope
     end), stops and prints a resume command — operator must approve
  4. BROWSER_SUBMIT is NEVER executed — BridgeSubmitRefused propagates

Usage:
  # Start a new session
  python3 run_apply_skill.py <job_url>

  # Dry run (no real browser calls — walk the step machine only)
  python3 run_apply_skill.py <job_url> --dry-run

  # Resume an interrupted session (after operator reviews the form)
  python3 run_apply_skill.py --resume <session_id>

  # List recent sessions
  python3 run_apply_skill.py --list-sessions

  # Skip Drive fetches + inbox (dev mode — uses stub rules, no browser for Drive)
  python3 run_apply_skill.py <job_url> --skip-drive --skip-inbox

Default flags: --skip-drive and --skip-inbox are ON unless --no-skip-drive /
--no-skip-inbox are given. This is safe because Drive file IDs are not yet
captured — run with full flags once the Drive refs file has real URLs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

from skill_runtime import (
    run_skill,
    save_state,
    list_sessions,
    SkillState,
    INTERRUPT,
    END,
    ABORT,
    PreconditionFailed,
    SkillNotFound,
)
from browser_bridge import execute_directives, BridgeSubmitRefused, bridge_alive

SKILL_NAME = "apply-job-session"

# Default resume path — the operator's own résumé on disk
RESUME_PATH = str(
    Path.home() / "Desktop/_resume_cache/Elijah_Wilkins_Resume_Portfolio.txt"
)


# ─── State helpers ───────────────────────────────────────────────────

def _is_directive_interrupt(state: SkillState) -> bool:
    """True when this INTERRUPT has pending browser directives to execute.
    Operator-review interrupts don't populate _pending_directives."""
    directives = state.data.get("_pending_directives")
    return bool(directives)


def _get_resume_step(state: SkillState) -> str | None:
    """After executing directives, which step should the skill resume at?

    Priority:
      1. state.data["_pending_step"] — explicitly set by _step_apply_one_job
      2. Last history entry's result["next"] — what the step intended before INTERRUPT
    """
    pending_step = state.data.get("_pending_step")
    if pending_step and pending_step not in (INTERRUPT, END, ABORT):
        return pending_step
    if state.history:
        last_result = state.history[-1].get("result") or {}
        nxt = last_result.get("next")
        if nxt and nxt not in (INTERRUPT, END, ABORT):
            return nxt
    return None


def _print_state(state: SkillState) -> None:
    print(f"\n{'─' * 60}")
    print(f"  skill:      {state.skill_name}")
    print(f"  session_id: {state.session_id}")
    print(f"  step:       {state.current_step}")
    print(f"  steps run:  {state.step_count}")
    print(f"  done:       {state.done} / aborted: {state.aborted}")
    if state.interrupt_reason:
        print(f"  reason:     {state.interrupt_reason[:120]}")
    if state.errors:
        print(f"  errors ({len(state.errors)}):")
        for e in state.errors[-3:]:
            print(f"    [{e['step']}] {str(e['error'])[:140]}")
    print(f"{'─' * 60}\n")


# ─── Core runner ─────────────────────────────────────────────────────

def run_session(
    job_url: str = None,
    *,
    dry_run: bool = False,
    resume_session_id: str = None,
    skip_drive_fetches: bool = True,
    skip_inbox: bool = True,
    candidate_urls: list[str] = None,
    max_directive_rounds: int = 50,
    verbose: bool = True,
) -> dict:
    """Run one job application session end-to-end.

    Parameters
    ----------
    job_url             : Single job URL to apply to (or None if using candidate_urls)
    dry_run             : Walk the step machine without real browser calls
    resume_session_id   : Resume an interrupted session by ID
    skip_drive_fetches  : Skip Drive doc fetches (uses stub rules — no real AI Query)
    skip_inbox          : Skip inbox reconcile step
    candidate_urls      : List of URLs (overrides job_url for the queue)
    max_directive_rounds: Safety cap on browser-directive loops

    Returns
    -------
    dict with keys:
      status:     "done" | "interrupted" | "submit_refused" | "aborted" | "error"
      session_id: str
      reason:     str  (set when status != "done")
      state:      SkillState  (final state; None on hard error before skill starts)
    """

    def log(msg: str) -> None:
        if verbose:
            print(msg)

    # ── Pre-flight: bridge health ─────────────────────────────────────
    if not dry_run:
        if not bridge_alive():
            msg = (
                "sensei bridge unreachable at http://127.0.0.1:8080\n"
                "  → make sure sensei is running (check ~/scripts/sensei_mcp_server.py)"
            )
            log(f"[run_apply_skill] ERROR: {msg}")
            return {"status": "error", "reason": msg, "session_id": None, "state": None}
        log("[run_apply_skill] bridge ✓")
    else:
        log("[run_apply_skill] DRY RUN — no real bridge calls")

    # ── Params ───────────────────────────────────────────────────────
    urls = candidate_urls or ([job_url] if job_url else [])
    params = {
        "job_url": job_url,
        "dry_run": dry_run,
        "skip_drive_fetches": skip_drive_fetches,
        "skip_inbox": skip_inbox,
        "candidate_urls": urls,
        "resume_path": RESUME_PATH,
    }

    log(f"[run_apply_skill] starting")
    log(f"  job_url:     {job_url or '(from queue)'}")
    log(f"  dry_run:     {dry_run}")
    log(f"  resume:      {resume_session_id or '(new session)'}")
    log(f"  skip_drive:  {skip_drive_fetches}   skip_inbox: {skip_inbox}")

    session_id = resume_session_id
    directive_rounds = 0

    # ── Main loop ────────────────────────────────────────────────────
    while True:

        # ── Run (or resume) the skill ─────────────────────────────────
        try:
            state = run_skill(
                SKILL_NAME,
                params=params,
                session_id=session_id,
                resume=(session_id is not None),
            )
        except PreconditionFailed as e:
            log(f"[run_apply_skill] PRECONDITION FAILED: {e}")
            log("  Tip: check ~/.master_ai_profile.json and ~/.master_ai_drive_refs.json")
            return {
                "status": "error",
                "reason": f"precondition failed: {e}",
                "session_id": session_id,
                "state": None,
            }
        except Exception as e:
            log(f"[run_apply_skill] run_skill raised: {type(e).__name__}: {e}")
            return {
                "status": "error",
                "reason": f"{type(e).__name__}: {e}",
                "session_id": session_id,
                "state": None,
            }

        session_id = state.session_id

        # ── Terminal: done ────────────────────────────────────────────
        if state.done:
            log("[run_apply_skill] ✓ session complete")
            _print_state(state)
            return {"status": "done", "session_id": session_id, "state": state}

        # ── Terminal: aborted ─────────────────────────────────────────
        if state.aborted:
            log("[run_apply_skill] ✗ session aborted")
            _print_state(state)
            return {
                "status": "aborted",
                "reason": state.interrupt_reason or "unknown",
                "session_id": session_id,
                "state": state,
            }

        # ── INTERRUPT ─────────────────────────────────────────────────
        if state.current_step != INTERRUPT:
            return {
                "status": "error",
                "reason": f"unexpected stop: current_step={state.current_step!r}",
                "session_id": session_id,
                "state": state,
            }

        directives = state.data.get("_pending_directives") or []

        # ── Case A: directive interrupt → execute + resume ────────────
        if _is_directive_interrupt(state):
            if directive_rounds >= max_directive_rounds:
                return {
                    "status": "error",
                    "reason": f"max_directive_rounds ({max_directive_rounds}) exceeded",
                    "session_id": session_id,
                    "state": state,
                }

            directive_rounds += 1
            log(f"\n[run_apply_skill] directive round {directive_rounds} "
                f"({len(directives)} actions) — {state.interrupt_reason or ''}")

            for d in directives:
                log(f"  ▸ {d[:110]}")

            # Execute (or simulate in dry_run)
            if dry_run:
                results = [
                    {
                        "directive": d,
                        "outcome": "dry_run",
                        "text": "[dry run — no real call]",
                        "kind": d.split(":")[0].strip(),
                    }
                    for d in directives
                ]
            else:
                try:
                    results = execute_directives(directives)
                except BridgeSubmitRefused as e:
                    log(f"\n[run_apply_skill] ⛔  SUBMIT REFUSED (safety gate)")
                    log(f"  {e}")
                    return {
                        "status": "submit_refused",
                        "reason": str(e),
                        "session_id": session_id,
                        "state": state,
                    }

            # Log outcomes
            for r in results:
                ok = r.get("outcome") in ("ok", "dry_run")
                icon = "✓" if ok else "✗"
                log(f"  {icon} {r.get('kind', '?')} → {r.get('outcome', '?')}")
                if not ok:
                    log(f"      error: {r.get('error') or r.get('reason', '')}")
                if r.get("text"):
                    excerpt = str(r["text"])[:200].replace("\n", " ")
                    log(f"      text:  {excerpt}")

            # Feed results back into state, clear consumed directives
            state.data["_last_directive_results"] = results
            state.data["_pending_directives"] = []

            # Advance current_step so run_skill(resume=True) doesn't loop on INTERRUPT
            resume_step = _get_resume_step(state)
            if not resume_step:
                return {
                    "status": "error",
                    "reason": (
                        "INTERRUPT: cannot determine resume step after directive execution. "
                        "Check _pending_step / history in state."
                    ),
                    "session_id": session_id,
                    "state": state,
                }

            state.current_step = resume_step
            save_state(state)
            log(f"  → resuming at step: {resume_step}\n")
            # Loop continues — run_skill(resume=True) fires from top

        # ── Case B: operator review / submit gate → stop ──────────────
        else:
            log("\n" + "═" * 62)
            log("  ⏸  OPERATOR REVIEW REQUIRED")
            log("═" * 62)
            log(f"  reason:     {state.interrupt_reason or '(none given)'}")
            log(f"  session_id: {session_id}")
            if state.data.get("current_url"):
                log(f"  job_url:    {state.data['current_url']}")
            log("")
            log("  The form (or listing) is in the browser. Review it, then:")
            log(f"  Resume:  python3 {Path(__file__).name} --resume {session_id}")
            log("═" * 62 + "\n")

            return {
                "status": "interrupted",
                "reason": state.interrupt_reason or "operator_review",
                "session_id": session_id,
                "state": state,
            }


# ─── CLI ─────────────────────────────────────────────────────────────

def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="run_apply_skill.py",
        description="Run the apply-job-session skill against a job URL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("job_url", nargs="?", help="Job URL (Indeed, ZipRecruiter, etc.)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Walk the step machine without real browser calls")
    parser.add_argument("--resume", metavar="SESSION_ID",
                        help="Resume an interrupted session")
    parser.add_argument("--skip-drive", action="store_true", default=True,
                        help="Skip Drive doc fetches (DEFAULT: on)")
    parser.add_argument("--no-skip-drive", dest="skip_drive", action="store_false",
                        help="Enable Drive fetches (requires real Drive file URLs)")
    parser.add_argument("--skip-inbox", action="store_true", default=True,
                        help="Skip inbox reconcile (DEFAULT: on)")
    parser.add_argument("--no-skip-inbox", dest="skip_inbox", action="store_false",
                        help="Enable inbox reconcile")
    parser.add_argument("--list-sessions", action="store_true",
                        help="List recent sessions for this skill")
    args = parser.parse_args()

    if args.list_sessions:
        try:
            sessions = list_sessions(SKILL_NAME)
        except SkillNotFound:
            print(f"No skill directory at ~/.master_ai_skills/{SKILL_NAME}/")
            return
        if not sessions:
            print("No sessions on record.")
            return
        hdr = f"{'session_id':<36} {'step':<25} {'done':<5} {'aborted':<7} steps"
        print(hdr)
        print("─" * len(hdr))
        for s in sessions:
            print(
                f"{s['session_id']:<36} {s['current_step']:<25} "
                f"{str(s['done']):<5} {str(s['aborted']):<7} {s['step_count']}"
            )
        return

    if not args.job_url and not args.resume:
        parser.error("provide a job_url or --resume SESSION_ID  (see --help)")

    result = run_session(
        job_url=args.job_url,
        dry_run=args.dry_run,
        resume_session_id=args.resume,
        skip_drive_fetches=args.skip_drive,
        skip_inbox=args.skip_inbox,
    )

    print(f"\nStatus:  {result['status']}")
    if result.get("reason"):
        print(f"Reason:  {result['reason']}")
    if result.get("session_id"):
        print(f"Session: {result['session_id']}")

    # Exit non-zero on failure
    if result["status"] not in ("done", "interrupted"):
        sys.exit(1)


if __name__ == "__main__":
    _main()
