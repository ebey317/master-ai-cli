#!/usr/bin/env python3
"""_mcp_tap.py

Debug wrapper to observe what Claude Code sends to a stdio MCP server.

Usage (for temporary debugging only):
  python3 /home/elijah/scripts/_mcp_tap.py --log /tmp/mcp_tap.log -- \
    python3 /home/elijah/scripts/sensei_mcp_server.py

This writes raw stdin lines to the log, forwards them to the child server,
and mirrors the child's stdout back to the parent.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from pathlib import Path


def _pump(src, dst, log: Path | None = None, prefix: str = ""):
    for line in src:
        if log:
            with log.open("a") as f:
                f.write(prefix + line.decode("utf-8", errors="replace"))
        dst.write(line)
        dst.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--", dest="dd", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    ns = ap.parse_args()
    if not ns.cmd:
        sys.stderr.write("no child command provided\n")
        return 2
    log = Path(ns.log)
    log.write_text("")  # truncate

    child = subprocess.Popen(
        ns.cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert child.stdin and child.stdout and child.stderr

    t_in = threading.Thread(target=_pump, args=(sys.stdin.buffer, child.stdin, log, "IN: "), daemon=True)
    t_out = threading.Thread(target=_pump, args=(child.stdout, sys.stdout.buffer, log, "OUT: "), daemon=True)
    t_err = threading.Thread(target=_pump, args=(child.stderr, sys.stderr.buffer, log, "ERR: "), daemon=True)
    t_in.start()
    t_out.start()
    t_err.start()

    return int(child.wait())


if __name__ == "__main__":
    raise SystemExit(main())

