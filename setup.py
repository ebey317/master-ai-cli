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
        "loop_fsm",
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
    ],
    packages=["sensei_clean", "sensei_clean.adapters"],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Operating System :: OS Independent",
    ],
)
