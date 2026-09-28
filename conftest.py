import importlib.util
import os
import shutil
import urllib.request

import pytest


def _chrome_available() -> bool:
    return any(
        shutil.which(b) for b in ("google-chrome", "chromium", "chromium-browser")
    )


def _ollama_available() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2).read()
        return True
    except Exception:
        return False


def _pupil_available() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=2).read()
        return True
    except Exception:
        return False


def _cloud_keys_available() -> bool:
    return bool(
        os.environ.get("GROQ_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("CEREBRAS_API_KEY")
    )


# Environmental test modules that should be skipped entirely when their runtime
# is missing. This keeps the clean-install pass rate honest without editing dozens
# of individual test files.
MODULE_SKIP_RULES = [
    (
        "test_pupil_api.py",
        _pupil_available,
        "Pupil HTTP server not reachable at 127.0.0.1:8080",
    ),
    (
        "test_browser_directives.py",
        _pupil_available,
        "browser bridge not reachable at 127.0.0.1:8080",
    ),
    (
        "test_chrome_headless_e2e.py",
        _chrome_available,
        "Chrome/Chromium binary not found",
    ),
    (
        "test_drive_inspect_handler.py",
        _chrome_available,
        "Chrome/Chromium binary not found",
    ),
    (
        "test_identity_self_reference.py",
        _ollama_available,
        "Ollama not reachable at 127.0.0.1:11434",
    ),
    (
        "test_orchestrate_prefix_in_envelope.py",
        _cloud_keys_available,
        "no cloud API keys configured",
    ),
    (
        "test_plan_block_emission.py",
        _ollama_available,
        "Ollama not reachable at 127.0.0.1:11434",
    ),
]


def pytest_collection_modifyitems(config, items):
    force_skip = os.environ.get("MCLI_SKIP_ENV_TESTS", "0") in (
        "1",
        "true",
        "True",
        "yes",
    )
    for item in items:
        module_name = item.module.__name__
        for rule_module, predicate, reason in MODULE_SKIP_RULES:
            target = rule_module[:-3]  # strip .py
            if module_name == target:
                if force_skip or not predicate():
                    item.add_marker(pytest.mark.skip(reason=reason))
                break


def pytest_configure(config):
    if os.environ.get("MCLI_SKIP_ENV_TESTS", "0") in ("1", "true", "True", "yes"):
        config.addinivalue_line(
            "markers", "env: environmental test skipped in clean-install mode"
        )


# ── Modules that cannot even be imported (2026-09-28) ───────────────────
#
# A collection error is fatal: pytest reports "Interrupted: N errors during
# collection" and runs NONE of the suite. Two modules were doing exactly
# that on every run, taking ~840 other tests down with them.
#
# tests/test_model_reasoning.py and tests/test_model_switch_reasoning.py
# exercise scripts/model_reasoning.py and scripts/model_switch.py, which
# import `from sensei import config` (and `agent`). There is no `sensei`
# package anywhere in this ecosystem -- not in the repo, not installed, not
# in ~/projects/sensei, which is a project directory rather than a Python
# package. These are orphaned ports whose dependency is absent.
#
# The fix is deliberately to ignore them rather than to stub `sensei`:
# a hand-written fake config/agent would turn these green while the feature
# still cannot run, which is the same "green tests, dead code" trap that
# the session_cwd import in master_ai.py hit in this same session. The
# tests re-enable themselves automatically the day a real `sensei` package
# becomes importable.
#
# The production modules are left untouched. Nothing but each other and
# these two test files imports them, so there is no behaviour to preserve.
_UNIMPORTABLE_MODULES = [
    (
        "tests/test_model_reasoning.py",
        "sensei",
        "scripts/model_reasoning.py imports `sensei.config`, which is not a "
        "package in this ecosystem",
    ),
    (
        "tests/test_model_switch_reasoning.py",
        "sensei",
        "scripts/model_switch.py imports `sensei.config` and `sensei.agent`, "
        "which are not a package in this ecosystem",
    ),
]


def _dependency_absent(module: str) -> bool:
    """True when `module` cannot be found on sys.path."""
    try:
        return importlib.util.find_spec(module) is None
    except (ImportError, ValueError):
        return True


collect_ignore = [
    path for path, dep, _reason in _UNIMPORTABLE_MODULES if _dependency_absent(dep)
]


def pytest_report_header(config):
    if collect_ignore:
        return [
            f"ignoring {len(collect_ignore)} test module(s) with absent "
            f"dependencies: {', '.join(collect_ignore)}"
        ]
    return []
