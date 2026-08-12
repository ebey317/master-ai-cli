# Split Fix Plan — master-ai-cli → 85+/100 Market Ready

**Date:** 2026-08-11  
**Goal:**
- `agent_standards_score()` ≥ 85/100.
- `master-ai --help` exits 0 with usage text.
- Clean install via `pip install -e .` works without requiring `install.sh`.
- Triage environmental tests so clean-install pass rate is honest.
- Add first-run setup + uninstall wizards with optional temporary GitHub Models assistant.

## Who Does What

| Agent | Scope |
|---|---|
| **Claude (cloud)** | `--help` fix, `agent_standards_score()` +1, initial triage ideas. |
| **Hermes (local)** | Packaging cleanup, environmental test guards (`conftest.py`), setup/uninstall wizards, README updates, final verification, push to GitHub. |

## Current State

| Metric | Value |
|---|---|
| `agent_standards_score()` | **95/100** (17 PASS, 2 WARN, 0 FAIL) |
| `master-ai --help` | ✅ prints usage, exits 0 |
| `master-ai --setup` | ✅ interactive setup with optional temporary GitHub Models assistant |
| `master-ai --uninstall` | ✅ interactive uninstall with optional temporary GitHub Models assistant |
| `pip install -e .` | ✅ works, clean wheel |
| Full pytest on dev machine | 635 passed / 48 failed / 14 skipped |
| Clean-install pytest (`MCLI_SKIP_ENV_TESTS=1`) | **620 passed / 23 failed / 55 skipped = 96.3%** |

## Tasks

### T1 — `--help` contract
- Fixed in `master_ai.py`: `main()` checks `-h`/`--help` before permissions wizard.
- Also added `--setup` and `--uninstall` entry points.

### T2 — Score ≥85
- Already at 95/100 via existing checks.

### T3 — Packaging cleanup
- `pyproject.toml` + `setup.py`: console scripts `master-ai` and `sensei`, `[tool.setuptools]`, `py-modules`.
- `MANIFEST.in`: excludes operator docs, shell scripts, JSON configs, tests, build artifacts.
- `.gitignore`: `*.egg-info/`.

### T4 — Environmental test triage
- `conftest.py` skips these modules when services are missing:
  - `test_pupil_api.py`, `test_browser_directives.py` → require `127.0.0.1:8080/health`
  - `test_chrome_headless_e2e.py`, `test_drive_inspect_handler.py` → require Chrome/Chromium
  - `test_identity_self_reference.py`, `test_plan_block_emission.py` → require Ollama
  - `test_orchestrate_prefix_in_envelope.py` → requires cloud API keys
- `MCLI_SKIP_ENV_TESTS=1` forces all of them to skip for CI/clean-machine reporting.

### T5 — First-run setup wizard (`setup_wizard.py`)
- Built-in ASCII splash / login header.
- Optional GitHub Models assistant: prompts for local/cloud provider keys, explains hybrid routing.
- Saves `~/.master_ai_keys` (chmod 600).
- Declares "GitHub AI disconnected" when setup ends.

### T6 — Uninstall wizard (`uninstall_wizard.py`)
- `master-ai --uninstall` with three levels:
  1. pip package + API keys/config (keeps Ollama)
  2. all user data + entry points (keeps Ollama)
  3. total wipe including Ollama + models
- Optional GitHub Models assistant for guided uninstall.

## Verification Commands

```bash
cd /home/elijah/projects/master-ai-cli
python3 -m pip install -e . >/dev/null 2>&1
python3 -c "import master_ai; print(master_ai.agent_standards_score())"
timeout 5 master-ai --help 2>&1; echo "exit=$?"
echo x | timeout 5 master-ai --setup
echo x | timeout 5 master-ai --uninstall
MCLI_SKIP_ENV_TESTS=1 python3 -m pytest -q --no-header --tb=no 2>&1 | tail -3
python3 -m build 2>&1 | tail -3
unzip -l dist/master_ai_cli-0.1.0-py3-none-any.whl | tail -5
```

## Final Results

- `agent_standards_score()` → **95/100**
- `master-ai --help` → **exit 0, usage printed**
- `pip install -e .` → ✅
- Clean-install test pass rate → **96.3%**
- Wheel contents → 63 files, no `.sh`/`.md`/`.json`/operator docs
- GitHub push → commit `d0d5d2b` on `master`

## Remaining Work (optional next pass)

- ~~Fix the 23 non-environmental test failures~~ — done (Claude, 2026-08-11
  session 2). See `FIX_PLAN_CLAUDE_23.md` for per-file diagnosis. Clean-install
  suite (`MCLI_SKIP_ENV_TESTS=1`) is now **643 passed / 0 failed / 55 skipped**.
  Full dev-machine suite is **658 passed / 24 failed** (all 24 remaining
  failures are the pre-existing environmental ones in `conftest.py`'s
  `MODULE_SKIP_RULES` — Pupil server / Chrome / Ollama not reachable on this
  run — not test bugs).
  - Two were real source bugs, not test staleness: `stt_server._api_parse_actions`
    referenced an undefined `chrome_extension` name (silently dropped every
    BROWSER_* action, not just screenshots); `orchestrate()`'s desktop-launch
    short-circuit wasn't gated behind `not _is_chrome_ext_automation` like its
    sibling short-circuits, so "open hypnotix" leaked past the chrome-extension
    automation turn's model-bearing-route guarantee.
  - One was a real hardening gap: `_read_path_ok` only checked the *resolved*
    path against the secret-path denylist, so a secret-named symlink pointing
    at a differently-named real file slipped through.
  - The rest were stale/non-portable tests: `test_router_golden.py` still
    pinned route names (`system_query`, `weather`) that were deliberately
    renamed/removed in earlier commits (`aad2762`, `953f31a`, `555cc09`);
    several tests hardcoded `/home/user/...` paths that don't exist on this
    machine; `test_api_handle_wedge.py` referenced the old single
    `_API_HANDLE_LOCK` after the 2026-05-14 per-lane lock redesign; and
    `test_phase7_10_tools.py` (plus `test_subagent_registry.py`, fixed
    alongside it) had a `sys.path` shadowing bug against an unrelated live
    deployment at `~/scripts` on this dev machine.
- Add TUI first-run splash that matches setup wizard.
- Make routing aware of GitHub Models token as a temporary provider for setup only.
