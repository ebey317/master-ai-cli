#!/usr/bin/env python3
"""Demonstration / smoke-test for portable temp utilities.

Run directly to verify behaviour; import in other scripts via:
    from scripts.temp_utils import get_sensei_tempdir, make_sensei_tempdir
"""
from __future__ import annotations
import os
import shutil
import sys
from pathlib import Path

# Ensure we can import the util when run from repo root
sys.path.insert(0, str(Path(__file__).parent))
from temp_utils import get_sensei_tempdir, make_sensei_tempdir, sensei_temp_path


def main() -> int:
    print("=== Sensei portable temp demo ===")
    print(f"Resolved temp root: {get_sensei_tempdir()}")

    # 1. Unique scratch dir for a tool run
    tool_dir = make_sensei_tempdir("tool-")
    (tool_dir / "output.txt").write_text("hello from tool")
    print(f"Tool scratch: {tool_dir} (exists={tool_dir.exists()})")

    # 2. Fixed subdir for persistent caches (e.g. downloaded models)
    cache_dir = make_sensei_tempdir(subdir="cache/models")
    print(f"Cache dir: {cache_dir}")

    # 3. Ad-hoc path under temp root
    log_path = sensei_temp_path("logs", "session.log", mkdir=True)
    log_path.write_text("session started\n")
    print(f"Log file: {log_path}")

    # Cleanup demo artefacts
    shutil.rmtree(tool_dir, ignore_errors=True)
    print("Cleaned up unique tool dir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
