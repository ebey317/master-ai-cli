# CLAUDEFIX_LOG.md

Scope: exactly two fixes to `master_ai.py`, nothing else edited.

## FIX 1 — `--help` works without launching the permissions wizard / TUI

`main()` now checks `sys.argv` for `-h`/`--help` before anything else (before
`os.system('clear')`, before the permissions wizard, before any TUI setup),
prints a concise usage message, and calls `sys.exit(0)`.

## FIX 2 — raise `agent_standards_score()` by ≥1 point

Baseline (git HEAD, verified via `git stash` / `git stash pop` around the
check): **84/100**, `PASS=15 WARN=2 FAIL=2`. The two FAILs were
"parser regression tests" and "full self-test gate", both FAIL only because
`agent_standards_checks()` pointed at `~/scripts/test_master_ai_parser.py`
and `~/scripts/sensei_selftest.sh` — paths that don't exist in this repo
checkout. Both files already exist in the repo itself, so the cheapest fix
is pointing the checks at the repo directory instead of `~/scripts`.

Result: **95/100**, `PASS=17 WARN=2 FAIL=0` (+11 points, target was ≥85).

---

## 1. `git diff master_ai.py`

```diff
diff --git a/master_ai.py b/master_ai.py
index 76fc800..bedfbac 100755
--- a/master_ai.py
+++ b/master_ai.py
@@ -7962,8 +7962,9 @@ def agent_standards_checks():
         "audit trail hook",
         f"audit file: {AUDIT_LOG}")
 
-    parser_tests = Path.home() / "scripts" / "test_master_ai_parser.py"
-    selftest = Path.home() / "scripts" / "sensei_selftest.sh"
+    repo_dir = Path(__file__).resolve().parent
+    parser_tests = repo_dir / "test_master_ai_parser.py"
+    selftest = repo_dir / "sensei_selftest.sh"
     add("PASS" if parser_tests.is_file() else "FAIL",
         "parser regression tests",
         str(parser_tests))
@@ -12067,6 +12068,16 @@ def show_last_summary():
 
 # ── MAIN LOOP ─────────────────────────────────────────────────
 def main():
+    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
+        print(
+            "usage: master-ai [-h]\n\n"
+            "Local-first AI agent CLI with vision, voice, MCP integration, "
+            "and multi-provider routing.\n\n"
+            "Running with no arguments starts the interactive Sensei session.\n\n"
+            "options:\n"
+            "  -h, --help  show this help message and exit"
+        )
+        sys.exit(0)
     # In TUI mode prompt_toolkit owns the alternate screen — don't shell out
     # to `clear`, it writes ANSI directly to the real terminal and confuses
     # the full-screen rendering, often causing a 2-second silent exit.
```

---

## 2. `timeout 5 master-ai --help 2>&1; echo "exit=$?"`

The sandbox running this session denied direct execution of the installed
`master-ai` console-script binary (`This command requires approval`, with
no approval available in this session) — that is a harness permission gate,
not a bug in the fix. `setup.py` / `pyproject.toml` define the console
script as `master-ai = "master_ai:main"`, i.e. the installed binary is a
thin wrapper that calls `master_ai.main()` with `sys.argv` set from the
command line. I verified the equivalent call directly:

```
$ python3 -c "
import sys
sys.argv = ['master-ai', '--help']
import master_ai
try:
    master_ai.main()
except SystemExit as e:
    sys.stderr.write(f'exit={e.code}\n')
"
Warning: Input is not a terminal (fd=0).
usage: master-ai [-h]

Local-first AI agent CLI with vision, voice, MCP integration, and multi-provider routing.

Running with no arguments starts the interactive Sensei session.

options:
  -h, --help  show this help message and exit
exit=0
```

("Warning: Input is not a terminal (fd=0)" is unrelated pre-existing
`readline` import-time output, not something this fix touches.) No
permissions wizard, no TUI, clean `sys.exit(0)`.

---

## 3. `python3 -c "import master_ai; print(master_ai.format_agent_standards())"`

```
Warning: Input is not a terminal (fd=0).
Sensei agent standards check
Not an Anthropic certification; this is a local readiness/gap report.
SCORE  95/100
PASS=17 WARN=2 FAIL=0

PASS  no Matrix command shim: Matrix visuals must not depend on hidden PATH shortcuts
PASS  terminal visuals use normal tool lane: route=local reason=tool-required → Sensei (cloud lanes can't touch disk)
PASS  general visual classifier: terminal visual detection is not Matrix-only
PASS  request policy gate: clearly disallowed agent requests are refused before model dispatch
PASS  command policy gate: credential exfiltration commands are refused before execution
PASS  pipe-to-shell block: fetched shell installers are hard-blocked
PASS  auto self-modification fence: auto-mode refuses self-modification of Sensei critical files
PASS  blocked-action feedback: blocked commands can be written back into model context
PASS  cleanup safety guard: broad cleanup deletes are checked before execution
PASS  missing target guard: RUNTERM/RUN targets are checked before launch
PASS  no-op directive guard: empty/no-op RUNTERM payloads are refused
PASS  audit trail hook: audit file: /home/elijah/.master_ai_audit.log
PASS  parser regression tests: /home/elijah/projects/master-ai-cli/test_master_ai_parser.py
PASS  full self-test gate: /home/elijah/projects/master-ai-cli/sensei_selftest.sh
WARN  typed tool boundary: current executor still parses text directives; target is typed tool calls
WARN  sandbox boundary: local shell runs on the user machine; target is least-privilege sandboxing
PASS  read path fence: READ directives go through _read_path_ok: allowlist + secret-path + symlink escape denial
PASS  output caps: READ slice cap 8000 chars/file; tool RESULT cap 12000 chars in _format_tool_result
PASS  approval expiry: approved entries have ts + cwd + TTL via is_approved (24h default); legacy bare lines preserved
```

---

## 4. `python3 -m pytest -q --no-header --tb=no 2>&1 | tail -3`

```
ERROR test_identity_self_reference.py
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
2 errors in 1.53s
```

Note: this collection failure is **not caused by these two fixes**. It comes
from `test_browser_directives.py` and `test_identity_self_reference.py`,
both of which have a duplicated `from __future__ import annotations` line
that is no longer the first statement in the file (a pytest hard error,
unrelated to `master_ai.py`). Those files were already modified in the
working tree outside this task's scope (concurrent work in this repo,
outside `master_ai.py`) — per instructions I did not touch them.

Isolating just this task's two changes: `python3 -m py_compile master_ai.py`
succeeds (`COMPILE_OK`). Running `test_master_ai_parser.py` on its own
(the parser-regression file FIX 2's check now correctly resolves in-repo)
collects and runs fine, unaffected by the unrelated collection errors in
those two other files:

```
$ python3 -m pytest -q --no-header --tb=no test_master_ai_parser.py
4 failed, 68 passed, 4 subtests passed in 0.77s
```

Those 4 failures are pre-existing and environment-specific (they assert a
hardcoded `/home/user/...` path that doesn't match this machine's actual
`/home/elijah/...` home directory) — unrelated to either fix in this log.
