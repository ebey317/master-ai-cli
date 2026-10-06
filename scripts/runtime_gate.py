#!/usr/bin/env python3
"""runtime_gate.py -- runtime smoke gate for master-ai-cli.

Catches the class of bug that green test suites miss. The motivating case:
374 tests passed while the real REPL crashed on launch with
"'NoneType' object has no attribute 'BC'". Tests import master_ai as a
module; users run it as __main__; the split modules resolve shared state via
sys.modules.get("master_ai"), which is None under __main__.

Three checks, each reproducing real runtime conditions:
  1. BOOT     - `python3 master_ai.py --help` launched as __main__, exactly as
                a user runs it: full import chain, must exit 0, no traceback.
  2. HEADLESS - two legs:
                (a) one real task through HeadlessRunner with a scripted model
                    reply; asserts parse -> execute -> artifact lands on disk,
                    and that the model is taught the directive format
                    (system prompt). The model itself is stubbed -- the gate
                    tests the framework loop, not model quality.
                (b) the real user path: `python3 master_ai.py --task ...
                    --headless` exec'd as __main__ in a subprocess with the
                    model stub pre-seeded. This is the seam that broke:
                    master_ai.py's __main__ block launched the TUI for
                    --headless flags instead of routing to headless_runner.
  3. SPLIT    - every extracted module imports and resolves its key functions
                with master_ai loaded as __main__ in a subprocess (the exact
                seam that broke). Not in the test process.

Concurrency rule: every heavy check first confirms (via pgrep) that no
other agent is running master_ai.py as a test process; if one is, the gate
refuses to run (exit 2) rather than piling on. Lijah's own live session at
~/scripts/master_ai.py is excluded -- it is not a test process and the gate
must never touch it.

Reaping rule: every spawned subprocess is wrapped in try/finally with
terminate-then-kill fallback, so no orphan or zombie survives a timeout or
an unexpected exception. The gate never detaches background processes.

Usage:
    python3 scripts/runtime_gate.py [--repo /path/to/master-ai-cli]

Exit 0 iff all checks pass; exit 2 if another agent's test is active.
Target: well under 2 minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap

REPO_DEFAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK_TIMEOUT = 60  # seconds per subprocess

# module -> key callables that must resolve (empty list = import-only check)
SPLIT_MODULES = {
    "orchestration": ["handle"],
    "dispatch": [],
    "routing": [],
    "context": [],
    "session_store": ["save_session"],
    "validation_gate": [],
    "runtime_state": [],
}

_SPLIT_DRIVER = textwrap.dedent(
    """\
    import sys, types, importlib, json

    repo = sys.argv[1]
    checks = json.loads(sys.argv[2])
    src_path = repo + "/master_ai.py"
    src = open(src_path).read()
    # Load the FULL source as __main__ -- including the real __main__ block --
    # with argv set so it takes the --help exit instead of launching the TUI.
    # This is the only way to test the real sys.modules alias (the exact seam
    # that broke): the driver must NOT seed the alias itself.
    mod = types.ModuleType("__main__")
    mod.__dict__["__name__"] = "__main__"
    mod.__dict__["__file__"] = src_path
    sys.modules["__main__"] = mod
    sys.path.insert(0, repo)
    sys.argv = ["master_ai.py", "--help"]
    try:
        exec(compile(src, src_path, "exec"), mod.__dict__)
    except SystemExit as e:
        assert e.code == 0, "--help exited %r" % (e.code,)
    # THE regression test for the __main__ crash: the real __main__ block must
    # have aliased this module, or extracted modules see None.
    assert sys.modules.get("master_ai") is mod, (
        "sys.modules['master_ai'] not aliased by __main__ block -- "
        "extracted modules would crash with NoneType errors")
    failures = []
    for mname, names in checks.items():
        try:
            m = importlib.import_module(mname)
        except Exception as e:
            failures.append("%s: import failed: %s: %s"
                            % (mname, type(e).__name__, e))
            continue
        for n in names:
            if not callable(getattr(m, n, None)):
                failures.append("%s.%s: missing or not callable" % (mname, n))
    if failures:
        print("SPLIT FAILURES:")
        for f in failures:
            print("  - " + f)
        sys.exit(1)
    print("SPLIT OK: __main__ alias live, %d modules import and resolve"
          % len(checks))
    """
)

# Driver for the real CLI headless path: execs master_ai.py as __main__ with
# the argv a user would type, after pre-seeding a stubbed model into
# headless_runner (so no API key or real LLM is needed). headless_runner is
# imported BEFORE exec so the __main__ block's `import headless_runner`
# reuses the stubbed module from sys.modules.
_HEADLESS_CLI_DRIVER = textwrap.dedent(
    """\
    import sys, types

    repo = sys.argv[1]
    probe = sys.argv[2]
    sys.path.insert(0, repo)
    import headless_runner

    _calls = {"n": 0}

    def fake_reply(self, history):
        _calls["n"] += 1
        if _calls["n"] == 1:
            assert history and history[0].get("role") == "system", (
                "first history message must be the system prompt, got: %r"
                % (history[0] if history else None,))
            assert "RUN:" in history[0].get("content", ""), (
                "system prompt does not teach the directive format")
            return "RUN: printf 'hello' > %s" % probe
        return "Task complete."

    # Class-level replacement (bound-method call): the runner calls
    # self._model_reply(self.history).
    headless_runner.HeadlessRunner._model_reply = fake_reply

    src_path = repo + "/master_ai.py"
    src = open(src_path).read()
    mod = types.ModuleType("__main__")
    mod.__dict__["__name__"] = "__main__"
    mod.__dict__["__file__"] = src_path
    sys.modules["__main__"] = mod
    sys.argv = ["master_ai.py", "--task",
                "Write the word hello to %s" % probe, "--headless"]
    try:
        exec(compile(src, src_path, "exec"), mod.__dict__)
    except SystemExit as e:
        if e.code not in (0, None):
            print("CLI headless exited %r" % (e.code,))
            sys.exit(1)
    print("CLI-HEADLESS OK: __main__ routed --task/--headless to headless_runner")
    """
)


class GateBusy(Exception):
    """Another agent's master_ai.py test process is active.

    The gate refuses to run concurrently with it (exit 2) instead of
    piling on and leaving the box slow with stray processes.
    """


def _foreign_master_ai_pids():
    """Pids of master_ai.py processes that belong to other agents' tests.

    Excludes Lijah's own live session at ~/scripts/master_ai.py -- that is
    his interactive auto-restart loop, not a test process, and the gate
    must never touch it. pgrep never matches itself.
    """
    r = subprocess.run(
        ["pgrep", "-af", r"master_ai\.py"],
        capture_output=True, text=True)
    pids = []
    for line in r.stdout.splitlines():
        pid_s, _, cmd = line.partition(" ")
        pid_s = pid_s.strip()
        if not pid_s.isdigit():
            continue
        if "/scripts/master_ai.py" in cmd:
            continue
        pids.append(pid_s)
    return pids


def _require_idle(what):
    pids = _foreign_master_ai_pids()
    if pids:
        raise GateBusy(
            "refusing %s: other master_ai.py test processes active (pids: %s); "
            "wait for them to finish and rerun the gate" % (what, ", ".join(pids)))


def _reap(proc):
    """Terminate, escalate to kill, and reap. Never raises, never orphans."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
    except Exception:
        pass
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def _run(cmd, **kw):
    timeout = kw.pop("timeout", CHECK_TIMEOUT)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=kw.pop("cwd", None),
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    finally:
        # Reap even on timeout or unexpected exception: terminate, then
        # kill. communicate() already reaps on success; _reap is a no-op
        # once proc.poll() is not None.
        _reap(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def check_boot(repo):
    """master_ai.py --help as __main__: full import chain, exit 0, no traceback."""
    _require_idle("BOOT check")
    print("[BOOT] launching master_ai.py --help as __main__ ...")
    try:
        r = _run([sys.executable, "master_ai.py", "--help"], cwd=repo)
    except subprocess.TimeoutExpired:
        return False, "--help timed out (>%ds), possible hang on startup" % CHECK_TIMEOUT
    out = (r.stdout or "") + (r.stderr or "")
    if "Traceback" in out:
        tail = "\n".join(out.strip().splitlines()[-8:])
        return False, "traceback on startup:\n" + tail
    if r.returncode != 0:
        return False, "exit code %d; output tail: %s" % (
            r.returncode, out.strip().splitlines()[-3:])
    return True, "clean startup as __main__ (rc=0, no traceback)"


def check_headless(repo):
    """Headless task, two legs: direct runner, then the real CLI path."""
    print("[HEADLESS] leg A: HeadlessRunner with stubbed model ...")
    if repo not in sys.path:
        sys.path.insert(0, repo)
    # Fresh import so the gate sees the current tree, not a stale module.
    for m in ("headless_runner", "typed_actions", "subagent_registry"):
        sys.modules.pop(m, None)
    try:
        import headless_runner
    except Exception as e:
        return False, "cannot import headless_runner: %s: %s" % (
            type(e).__name__, e)

    probe = "/tmp/gate_probe.txt"
    try:
        os.remove(probe)
    except OSError:
        pass

    runner = headless_runner.HeadlessRunner(
        task="Write the word hello to %s" % probe, max_turns=3)
    calls = {"n": 0}

    def fake_reply(history):
        calls["n"] += 1
        if calls["n"] == 1:
            first = history[0] if history else {}
            assert first.get("role") == "system", \
                "first history message must be the system prompt, got: %r" % (
                    first.get("role"),)
            assert "RUN:" in first.get("content", ""), \
                "system prompt does not teach the directive format"
            return "RUN: printf 'hello' > %s" % probe
        return "Task complete."

    runner._model_reply = fake_reply
    try:
        out = runner.run()
    except Exception as e:
        return False, "headless loop crashed: %s: %s" % (type(e).__name__, e)

    if not os.path.exists(probe):
        return False, "artifact not created at %s; loop output tail: %r" % (
            probe, out[-400:] if isinstance(out, str) else out)
    content = open(probe).read()
    if content != "hello":
        return False, "artifact content wrong: %r (expected 'hello')" % content

    _require_idle("HEADLESS leg B")
    print("[HEADLESS] leg B: real CLI `master_ai.py --task ... --headless` ...")
    cli_probe = "/tmp/gate_cli_probe.txt"
    try:
        os.remove(cli_probe)
    except OSError:
        pass
    try:
        r = _run([sys.executable, "-c", _HEADLESS_CLI_DRIVER, repo, cli_probe],
                 cwd=repo)
    except subprocess.TimeoutExpired:
        return False, "CLI headless driver timed out (>%ds)" % CHECK_TIMEOUT
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-10:])
        return False, "CLI headless path failed:\\n" + tail
    if not os.path.exists(cli_probe):
        return False, ("CLI headless ran but artifact missing at %s; "
                       "output tail: %r") % (cli_probe, out.strip()[-400:])
    content = open(cli_probe).read()
    if content != "hello":
        return False, "CLI artifact content wrong: %r (expected 'hello')" % content
    return True, ("leg A: parse -> execute -> artifact verified (%s == 'hello'); "
                  "leg B: __main__ routed --task/--headless to headless_runner, "
                  "artifact verified (%s == 'hello')") % (probe, cli_probe)


def check_split(repo):
    """Extracted modules resolve under a real __main__ load (subprocess)."""
    _require_idle("SPLIT check")
    print("[SPLIT] loading master_ai as __main__ in a subprocess ...")
    try:
        r = _run(
            [sys.executable, "-c", _SPLIT_DRIVER, repo, json.dumps(SPLIT_MODULES)],
            cwd=repo,
        )
    except subprocess.TimeoutExpired:
        return False, "split driver timed out (>%ds)" % CHECK_TIMEOUT
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-10:])
        return False, "split check failed:\\n" + tail
    return True, "all extracted modules import and resolve under __main__"


def main():
    ap = argparse.ArgumentParser(description="master-ai-cli runtime gate")
    ap.add_argument("--repo", default=REPO_DEFAULT)
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)
    if not os.path.exists(os.path.join(repo, "master_ai.py")):
        print("FAIL: not a master-ai-cli repo: %s" % repo)
        return 1

    results = []
    for name, fn in (("BOOT", check_boot),
                     ("HEADLESS", check_headless),
                     ("SPLIT", check_split)):
        try:
            passed, detail = fn(repo)
        except GateBusy as e:
            print("FAIL: %s" % e)
            return 2
        except AssertionError as e:
            passed, detail = False, "assertion: %s" % e
        except Exception as e:
            passed, detail = False, "%s: %s" % (type(e).__name__, e)
        results.append((name, passed, detail))

    print()
    ok = True
    for name, passed, detail in results:
        print("%s %s" % ("PASS" if passed else "FAIL", name))
        for line in detail.splitlines():
            print("     " + line)
        ok = ok and passed
    print()
    print("GATE %s" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
