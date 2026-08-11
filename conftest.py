import os
import shutil
import urllib.request

import pytest


def _chrome_available() -> bool:
    return any(shutil.which(b) for b in ("google-chrome", "chromium", "chromium-browser"))


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
    ("test_pupil_api.py", _pupil_available, "Pupil HTTP server not reachable at 127.0.0.1:8080"),
    ("test_browser_directives.py", _pupil_available, "browser bridge not reachable at 127.0.0.1:8080"),
    ("test_chrome_headless_e2e.py", _chrome_available, "Chrome/Chromium binary not found"),
    ("test_drive_inspect_handler.py", _chrome_available, "Chrome/Chromium binary not found"),
    ("test_identity_self_reference.py", _ollama_available, "Ollama not reachable at 127.0.0.1:11434"),
    ("test_orchestrate_prefix_in_envelope.py", _cloud_keys_available, "no cloud API keys configured"),
    ("test_plan_block_emission.py", _ollama_available, "Ollama not reachable at 127.0.0.1:11434"),
]


def pytest_collection_modifyitems(config, items):
    force_skip = os.environ.get("MCLI_SKIP_ENV_TESTS", "0") in ("1", "true", "True", "yes")
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
        config.addinivalue_line("markers", "env: environmental test skipped in clean-install mode")

