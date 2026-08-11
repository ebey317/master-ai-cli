# Market-Readiness Audit — master-ai-cli (Claude)

Date: 2026-08-11

## 1. `ls -la`

Repo root contains ~150 files: the intended package modules (`master_ai.py` at 664KB, `router.py`, `harvest.py`, `hooks.py`, `typed_actions.py`, `observability.py`, `subagent_registry.py`, etc.), packaging files (`pyproject.toml`, `setup.py`, `requirements.txt`), and a very large amount of operator-specific material that has no place in a pip-installable product: `.sh` install/tuning scripts (`apply_earlyoom.sh`, `apply_i915_safety.sh`, `apply_ufw_ports.sh`, `system_tune.sh`, `matrix_rain.sh`, `deep_clean.sh`, `endurance_day.sh`, `benchmark_sensei.sh`, `competitor_benchmark.sh` at 25KB, `jobseeker.sh`), planning docs (`APOCALYPSE_MECHANISM_OPTIONS.md`, `PROJECTS.md`, `LAYER_MAP.md`, `LINKS.md`), and 60+ `test_*.py` files sitting at repo root (no `tests/` packaging, despite a `tests/` dir also existing separately — duplication). `master_ai_voice.json`, `Modelfile-master-ai` (41KB), `sd-client-config.json` also ship at root with no clear packaging story. `AUDIT.md` and `CHANGES.md` already exist from a prior audit pass.

Full listing omitted here for length — see raw output; 170+ entries at repo root, most unrelated to the pip package surface declared in `setup.py`/`pyproject.toml`.

## 2. `pyproject.toml`

```toml
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "master-ai-cli"
version = "0.1.0"
description = "Local-first AI agent CLI with vision, voice, MCP integration, and multi-provider routing"
readme = "README.md"
license = {text = "MIT"}
requires-python = ">=3.10"
dependencies = ["ddgs>=0.8.0"]

[project.optional-dependencies]
gdrive = ["google-api-python-client>=2.0.0", "google-auth-oauthlib>=1.0.0"]
voice = ["openai-whisper>=20231117", "pyaudio>=0.2.14"]
vision = ["opencv-python>=4.8.0", "pillow>=10.0.0"]
dev = ["pytest>=7.0.0", "pytest-cov>=4.0.0"]

[project.scripts]
master-ai = "master_ai:main"
sensei = "master_ai:main"
```

No `[tool.setuptools]` packages/py-modules declaration — relies on `setup.py`'s `py_modules=[...]` list (see below) since pyproject.toml alone declares no source layout. Mixing `pyproject.toml` (PEP 621 metadata) with a `setup.py` that duplicates and extends the module list is fragile — two sources of truth for the same package surface.

## 3. `setup.py`

```python
from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="master-ai-cli",
    version="0.1.0",
    ...
    py_modules=[
        "ab_few_shot", "approval_queue", "capabilities", "claf_cli_integration",
        "completion", "extract_html", "harvest", "hooks", "iprice", "loop_fsm",
        "master_ai", "observability", "prewarm_master_ai", "prompt_versions",
        "router", "sensei_clean", "sensei_clean_app", "sensei_clean_web",
        "sensei_extractor", "sensei_memory_index", "sensei_native_host",
        "sensei_reasoning_loop", "sensei_reflect", "sensei_tool_detector",
        "sensei_tui", "setup_email", "skill_runtime", "slideshow", "slideshow_uninstall",
        "stt_server", "subagent_registry", "tts_server", "typed_actions",
        "url_grounding", "verifiers", "whereisit",
    ],
    packages=["sensei_clean", "sensei_clean.adapters"],
    python_requires=">=3.10",
    install_requires=["ddgs>=0.8.0"],
    extras_require={...},
    entry_points={
        "console_scripts": [
            "master-ai=master_ai:main",
            "sensei=master_ai:main",
        ],
    },
    ...
)
```

`find_packages` is imported but unused — dead import. `py_modules` lists 34 top-level modules manually (error-prone; new modules silently won't ship unless someone remembers to add them here — e.g. `router.py`, `hooks.py` are listed, but `stt_server.py`, `sensei_tui.py` at 194KB/49KB ship as flat modules polluting `site-packages` root on install). Both `master-ai` and `sensei` are aliases for the same `master_ai:main` entry point — no differentiation between the two despite CLAUDE.md describing them as distinct experiences ("`master` = portal/menu", "`sensei` = direct agent terminal").

## 4. `pip install -e .`

```
Preparing editable metadata (pyproject.toml): finished with status 'done'
Requirement already satisfied: ddgs>=0.8.0 ...
Building wheels for collected packages: master-ai-cli
  Building editable for master-ai-cli (pyproject.toml): finished with status 'done'
  Created wheel for master-ai-cli: filename=master_ai_cli-0.1.0-0.editable-py3-none-any.whl
Successfully installed master-ai-cli-0.1.0
```

Install succeeds cleanly. No build errors.

## 5. `which master-ai; which sensei`

```
/home/elijah/.local/bin/master-ai
/home/elijah/.local/bin/sensei
```

Both entry points are correctly installed on `PATH`.

## 6. `master-ai --help`

**Critical finding: `--help` is not implemented.** Running `master-ai --help` does not print usage text or exit — it launches the full interactive first-run onboarding wizard ("🔐 Permissions Walkthrough", "Permission 1 of 8 [required] — Shell Command Execution") and blocks waiting on stdin (`Warning: Input is not a terminal (fd=0)`, then presents a `1) Yes / 2) Yes to All` prompt). `--help` is never special-cased before the interactive flow starts. This means:
- `pip install` → `master-ai --help` (the first thing any evaluator, packaging bot, or CI system runs) hangs instead of exiting 0.
- Any automated smoke test, `man`-page generator, or shell-completion tool that probes `--help` will time out or force-answer an 8-step permissions wizard non-interactively.

This alone is disqualifying for a pip-distributed CLI — `--help`/`-h` is a baseline expectation enforced by nearly every packaging/CI convention.

## 7. `pytest -q --no-header --tb=no`

```
FAILED test_router_golden.py::WeatherRouting::test_whats_the_weather_short_circuits
FAILED test_router_golden.py::HarvestRecordedOnDeterministicShortCircuit::test_handle_records_harvest_for_system_query_route
ERROR test_plan_block_emission.py::PlanBlockEmissionTests::test_multi_step_emits_plan_block_with_required_sections
ERROR test_plan_block_emission.py::PlanBlockEmissionTests::test_single_step_does_not_emit_plan_block
56 failed, 638 passed, 3 skipped, 2 warnings, 2 errors, 14 subtests passed in 90.34s
```

56 of 694 collected non-skipped tests fail, plus 2 collection/setup errors, on a clean editable install with no code changes. A product with a failing baseline test suite (8% failure rate) is not shippable as-is — either the suite is stale/flaky (needs pruning) or there are real regressions; either way this needs triage before release.

## 8. `agent_standards_score()`

```
Warning: Input is not a terminal (fd=0).
84
```

Score is 84 (out of 100, based on repo's own rubric — CLAUDE.md's 2026-06-14 roadmap target is 105/100 with 0 WARN/FAIL and explicitly states "Do NOT certify as world-ready" until all 5 listed gaps — typed dispatch, sandbox boundary, read-fence TTL, output caps, approval expiry — are closed). 84 is below the project's own bar, and the CLAUDE.md notes as of 2026-05-11 the score was 95 with typed dispatch and sandboxing still WARN — 84 suggests regression or a different scoring path than what CLAUDE.md describes; needs reconciling against the actual `agent_standards_score()` implementation before trusting either number.

## 9. Hardcoded `/home/elijah` paths in `*.py`

No matches. Clean — no hardcoded absolute paths to the operator's home directory found in any `.py` file at repo root or below.

## 10. Hardcoded secrets/keys/tokens in `*.py`

```
test_drive_inspect_handler.py:import secrets
test_drive_inspect_handler.py:        key = base64.b64encode(secrets.token_bytes(16)).decode()
test_drive_inspect_handler.py:        mask = secrets.token_bytes(4)
test_drive_inspect_handler.py:            self.sock.sendall(bytes([0x88, 0x80]) + secrets.token_bytes(4))
test_sensei_clean_open.py:                          for c in choices]  # extract display name token
test_browser_directives.py:TOKEN_FILE = Path.home() / ".master_ai_extension_token"
test_browser_directives.py:def _read_token():
test_browser_directives.py:            "X-Master-AI-Token": _read_token(),
test_browser_directives.py:    print(f"[test_browser_directives] lane={LANE_LABEL}, base={BASE_URL}, token={'set' if _read_token() else 'empty'}")
test_chrome_headless_e2e.py:import secrets
```

No literal hardcoded secret values found in the first 10 hits — matches are Python's `secrets` stdlib module and token-file/token-header plumbing that reads from `~/.master_ai_extension_token` at runtime rather than embedding a value. This is a `head -10` sample only; not a full scan (grep found more than 10 total matches across the repo and this only shows the first page — a full audit should run the grep without `head` before shipping).

## Verdict: **Not ready**

1. **`--help` hangs on an interactive permissions wizard instead of exiting.** This breaks the single most basic CLI contract and will fail any automated install-and-smoke-test pipeline, including PyPI's own trusted-publishing CI checks and any user's first command after `pip install`.
2. **56 failing tests / 2 errors out of ~694** on a fresh, unmodified install — no confidence the packaged code works as intended, and no CI gate currently blocks a release with this failure rate.
3. **Packaging is fragile and scope is unclear**: `setup.py`'s manually-maintained `py_modules` list (with an unused `find_packages` import) is the real source of truth for what ships, `pyproject.toml` declares none of it, and the repo ships 150+ operator-specific files (tuning scripts, personal planning docs, dev tests at root) alongside the installable surface with no `MANIFEST.in`/sdist exclusion evident — a `pip install .` from a source tarball would need verification that none of this leaks into the wheel. Additionally the project's own `agent_standards_score()` (84) sits below the 95+ bar the project's CLAUDE.md says is required before calling anything "world-ready."
