from setuptools import setup

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="master-ai-cli",
    version="0.1.0",
    author="Elijah Wilkins",
    description="Local-first AI agent CLI with vision, voice, MCP integration, and multi-provider routing",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/ebey317/master-ai-cli",
    py_modules=[
        "ab_few_shot", "approval_queue", "capabilities", "claf_cli_integration",
        "completion", "extract_html", "harvest", "hooks", "iprice", "loop_fsm",
        "master_ai", "observability", "prewarm_master_ai", "prompt_versions",
        "router", "sensei_clean", "sensei_clean_app", "sensei_clean_web",
        "sensei_extractor", "sensei_memory_index", "sensei_native_host",
        "sensei_reasoning_loop", "sensei_reflect", "sensei_tool_detector",
        "sensei_tui", "setup_email", "setup_wizard", "skill_runtime", "slideshow", "slideshow_uninstall",
        "stt_server", "subagent_registry", "tts_server", "typed_actions", "uninstall_wizard",
        "url_grounding", "verifiers", "whereisit",
    ],
    packages=["sensei_clean", "sensei_clean.adapters"],
    python_requires=">=3.10",
    install_requires=[
        "ddgs>=0.8.0",
    ],
    extras_require={
        "gdrive": [
            "google-api-python-client>=2.0.0",
            "google-auth-oauthlib>=1.0.0",
        ],
        "voice": [
            "openai-whisper>=20231117",
            "pyaudio>=0.2.14",
        ],
        "vision": [
            "opencv-python>=4.8.0",
            "pillow>=10.0.0",
        ],
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "master-ai=master_ai:main",
            "sensei=master_ai:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Operating System :: OS Independent",
    ],
)