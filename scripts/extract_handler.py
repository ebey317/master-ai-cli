#!/usr/bin/env python3
"""Dev helper: extract a self-contained `if` block out of main()'s if-chain
into a module-level `_handle_*` function, and leave a delegating call.

Used to shrink main() (3,600 lines) by pulling whole REPL command handlers
out, following the pattern already established for _handle_fallback_cmd,
_restore_reload_carry and _handle_schedule_cmd.

Not part of the shipped code. Run from the repo root:

    python3 scripts/extract_handler.py <name> <guard-ast-source> [--report]

Guard is the `if` test that selects this handler, e.g.
"lo == 'hooks' or lo.startswith('hooks ')".
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "master_ai.py"


def find_block(tree, guard_src: str):
    """Locate the top-level If in main() whose test unparses to guard_src."""
    main = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    want = " ".join(guard_src.split())
    for node in ast.walk(main):
        if not isinstance(node, ast.If):
            continue
        try:
            got = " ".join(ast.unparse(node.test).split())
        except Exception:
            continue
        if got == want:
            # Only accept blocks that end in `continue` — that is the
            # handler shape, and it proves the block is terminal for the
            # command rather than a fall-through condition.
            if any(isinstance(c, ast.Continue) for c in ast.walk(node)):
                return node
    return None


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    name, guard = sys.argv[1], sys.argv[2]
    report_only = "--report" in sys.argv

    src = TARGET.read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = find_block(tree, guard)
    if node is None:
        print(f"NO MATCH for guard: {guard}")
        return 1

    lines = src.split("\n")
    start, end = node.lineno - 1, node.end_lineno  # 0-based, end exclusive
    body = lines[start:end]
    print(f"block: lines {start + 1}-{end} ({end - start} lines)")
    print(f"guard: {guard}")

    if report_only:
        return 0

    # Strip the `if <guard>:` header and the trailing `continue`.
    inner = body[1:]
    while inner and not inner[-1].strip():
        inner.pop()
    # Match `continue` allowing a trailing comment ("continue  # unreachable").
    trailing = inner[-1].strip() if inner else ""
    if not re.fullmatch(r"continue\s*(#.*)?", trailing):
        raise SystemExit(f"expected a trailing `continue`, found: {inner[-1]!r}")
    inner.pop()

    # De-indent one level (8 spaces) to sit at module scope.
    dedented = []
    for line in inner:
        if line.startswith(" " * 8):
            dedented.append(line[8:])
        elif not line.strip():
            dedented.append("")
        else:
            raise SystemExit(f"unexpected indent in block line: {line!r}")
    new_body = "\n".join(dedented).rstrip()

    # A handler body can contain an EARLY `continue` too (e.g. a usage
    # message that bails out before the work). Those become `return True`,
    # since the command was still consumed. Left as `continue` they compile
    # as a syntax error outside a loop.
    early_continues = sum(
        1 for line in new_body.split("\n") if line.strip() == "continue"
    )
    if early_continues:
        new_body = re.sub(r"^(\s*)continue$", r"\1return True", new_body, flags=re.M)
        print(f"converted {early_continues} inner `continue` -> `return True`")

    call = f"        if {name}(lo, cmd):\n            continue\n"
    src = src.replace("\n".join(body) + "\n", call, 1)

    func = (
        f"\n\n# ── `{name[1:].replace('handle_', '').replace('_cmd', '')}` REPL command "
        f"(2026-09-28) ──────────────────────────────────\n"
        f"# Extracted verbatim out of main()'s inline if-chain. Pure move.\n"
        f"def {name}(lo, cmd):\n"
        f'    """Handle one `{guard}`. Returns True if it was consumed."""\n'
        f"    if not ({guard}):\n"
        f"        return False\n"
        f"{new_body}\n"
        f"    return True\n"
    )
    src = src.replace("\ndef main():", func + "\n\ndef main():", 1)
    TARGET.write_text(src, encoding="utf-8")
    print(f"extracted -> {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
