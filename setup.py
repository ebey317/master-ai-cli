from setuptools import setup

# 2026-09-29: packaging metadata (name, version, description, readme,
# requires-python, dependencies, extras, console scripts) lives in
# pyproject.toml [project]. Once [project] exists, setuptools ignores
# the setup.py equivalents, so keeping them here only hides drift — the
# thread-factory scaffold (9d3c129) proved it: a bare [project]
# silently dropped the dev extra and install_requires from CI's
# `pip install -e .[dev]` and renamed the installed product. This file
# now carries only what [project] cannot express: the module/package
# lists, author, url, and classifiers.

setup(
    author="Elijah Wilkins",
    url="https://github.com/ebey317/master-ai-cli",
    py_modules=[
        "ab_few_shot",
        "approval_queue",
        "capabilities",
        "claf_cli_integration",
        "completion",
        "extract_html",
        "gate",
        "harvest",
        "hooks",
        "iprice",
        "master_ai",
        "observability",
        "prewarm_master_ai",
        "prompt_versions",
        "router",
        "sandbox",
        "sensei_clean_cli",
        "sensei_clean_app",
        "sensei_clean_web",
        "sensei_extractor",
        "sensei_memory_index",
        "sensei_native_host",
        "sensei_reasoning_loop",
        "sensei_reflect",
        "sensei_tables",
        "sensei_tool_detector",
        "sensei_tui",
        "setup_email",
        "setup_wizard",
        "skill_runtime",
        "slideshow",
        "slideshow_uninstall",
        "stt_server",
        "subagent_registry",
        "thread_factory",
        "tts_server",
        "typed_actions",
        "uninstall_wizard",
        "url_grounding",
        "verifiers",
        "whereisit",
        "delegate_runner",
        # 2026-09-29: every module below is imported by code already listed
        # above but was missing here, so `pip install .` produced a tree that
        # raised ModuleNotFoundError on import. `keychain_kv` and
        # `perpetual_review` are hard module-level imports (gate, master_ai,
        # setup_wizard) and broke a clean install outright. The rest are
        # imported lazily inside functions, so they failed only on the feature
        # that needed them -- a quieter and more confusing failure. Found by
        # walking the AST of every packaged module for local imports that
        # resolved to a file in this repo but appeared in neither list.
        "action_validation",
        "aies_tutor",
        "doc_reader",
        "free_model_picker",
        "hardware_model",
        "keychain_kv",
        "perpetual_review",
        "plan_jev_gate",
        "plan_slots",
        "retrieval",
        "sensei_mcp_client",
        "skill_improve_helpers",
        "system_capability_scan",
        "telegram_client",
        "tinyfish_client",
        "embeddings",
        "learning_loop",
        "skill_author",
        "skill_marketplace",
        "session_harvester",
    ],
    # `scripts` is a real package (has __init__.py) and `master_ai` imports it
    # at module level, so it has to be installed as a package -- it cannot be
    # expressed in py_modules. `scripts.tools` has no __init__.py, so it is
    # intentionally not listed; naming it would make the build fail.
    packages=[
        "scripts",
        "sensei_clean",
        "sensei_clean.adapters",
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Operating System :: OS Independent",
    ],
)
