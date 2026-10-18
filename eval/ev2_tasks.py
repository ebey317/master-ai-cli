"""Eval v2 task suite — execution-verified, Terminal-Bench style.

Every task: a prompt (absolute paths injected), a setup() that seeds the
scratch workdir, and a check(workdir, reply) that verifies END STATE on disk
(or the reply, for pure-read tasks). The model's claims are never trusted.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

# ── helpers the checks share ──────────────────────────────────────────


def _read(wd: Path, name: str) -> str | None:
    p = wd / name
    return p.read_text() if p.is_file() else None


def _norm(text: str | None) -> str:
    return (text or "").strip()


def _run(cmd: list[str], cwd: Path, timeout: int = 20) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return f"<check-run-error: {exc}>"


# ── the tasks ─────────────────────────────────────────────────────────
# Each: name, category, prompt(wd) -> str, setup(wd), check(wd, reply)

TASKS: list[dict] = [
    {
        "name": "create_exact",
        "category": "files",
        "prompt": lambda wd: (
            f"Use the CREATE directive with a <<<CONTENT block to create "
            f"{wd}/hello.txt containing exactly the text EV2_HELLO_42."
        ),
        "setup": lambda wd: None,
        "check": lambda wd, reply: (
            _norm(_read(wd, "hello.txt")) == "EV2_HELLO_42",
            f"content={_norm(_read(wd, 'hello.txt'))!r}",
        ),
    },
    {
        "name": "edit_replace",
        "category": "edit",
        "prompt": lambda wd: (
            f"In {wd}/config.ini use the EDIT directive to replace the text "
            f"'mode=debug' with 'mode=release'."
        ),
        "setup": lambda wd: (wd / "config.ini").write_text("mode=debug\nport=8080\n"),
        "check": lambda wd, reply: (
            "mode=release" in (_read(wd, "config.ini") or "")
            and "port=8080" in (_read(wd, "config.ini") or ""),
            f"content={_read(wd, 'config.ini')!r}",
        ),
    },
    {
        "name": "append_line",
        "category": "files",
        "prompt": lambda wd: (
            f"Append the single line EV2_APPENDED to {wd}/log.txt "
            f"(create the file if it does not exist)."
        ),
        "setup": lambda wd: None,
        "check": lambda wd, reply: (
            "EV2_APPENDED" in (_read(wd, "log.txt") or ""),
            f"content={_read(wd, 'log.txt')!r}",
        ),
    },
    {
        "name": "shell_pipe_count",
        "category": "shell",
        "prompt": lambda wd: (
            f"Run a shell command that counts the bytes of the literal string "
            f"abcdef and writes ONLY the resulting number (no other text) "
            f"into {wd}/count.txt."
        ),
        "setup": lambda wd: None,
        "check": lambda wd, reply: (
            _norm(_read(wd, "count.txt")) == "6",
            f"content={_norm(_read(wd, 'count.txt'))!r}",
        ),
    },
    {
        "name": "git_roundtrip",
        "category": "git",
        "prompt": lambda wd: (
            f"Create a git repository at {wd}/repo, put a file note.txt in it "
            f"with the content v1, and commit it with the message "
            f"'ev2 first commit'. Then run git log --oneline in that repo and "
            f"report the output."
        ),
        "setup": lambda wd: (wd / "repo").mkdir(exist_ok=True),
        "check": lambda wd, reply: (
            (wd / "repo" / ".git").is_dir()
            and (wd / "repo" / "note.txt").read_text().strip() == "v1"
            and "ev2 first commit" in _run(["git", "log", "--oneline"], wd / "repo"),
            f"git_log={_run(['git', 'log', '--oneline'], wd / 'repo').strip()!r}",
        ),
    },
    {
        "name": "python_print",
        "category": "code",
        "prompt": lambda wd: (
            f"Create {wd}/add.py that prints the sum of 40 and 2, then run it "
            f"with python3 and save its output into {wd}/sum.txt."
        ),
        "setup": lambda wd: None,
        "check": lambda wd, reply: (
            _norm(_read(wd, "sum.txt")) == "42",
            f"sum.txt={_norm(_read(wd, 'sum.txt'))!r}",
        ),
    },
    {
        "name": "json_sum",
        "category": "data",
        "prompt": lambda wd: (
            f"Read {wd}/data.json and write the sum of the numbers in its "
            f"'items' list into {wd}/total.txt (just the number)."
        ),
        "setup": lambda wd: (wd / "data.json").write_text(
            json.dumps({"items": [1, 2, 3, 4]})
        ),
        "check": lambda wd, reply: (
            _norm(_read(wd, "total.txt")) == "10",
            f"total.txt={_norm(_read(wd, 'total.txt'))!r}",
        ),
    },
    {
        "name": "read_answer",
        "category": "extraction",
        "prompt": lambda wd: (
            f"Read {wd}/memo.txt and tell me the launch code written in it."
        ),
        "setup": lambda wd: (wd / "memo.txt").write_text(
            "Launch window is Tuesday.\nThe launch code is ORANGE-7.\n"
        ),
        "check": lambda wd, reply: (
            "ORANGE-7" in (reply or ""),
            f"reply_has_code={'ORANGE-7' in (reply or '')}",
        ),
    },
    {
        "name": "bugfix_script",
        "category": "code",
        "prompt": lambda wd: (
            f"{wd}/greet.py should print exactly 42 but it prints something "
            f"else. Fix it (edit the file) and run it, saving its output into "
            f"{wd}/out.txt."
        ),
        "setup": lambda wd: (wd / "greet.py").write_text("print(41)\n"),
        "check": lambda wd, reply: (
            _norm(_read(wd, "out.txt")) == "42"
            and "42" in (_read(wd, "greet.py") or ""),
            f"out.txt={_norm(_read(wd, 'out.txt'))!r}",
        ),
    },
    {
        "name": "csv_sum",
        "category": "data",
        "prompt": lambda wd: (
            f"Compute the sum of the numeric column in {wd}/sales.csv and "
            f"write only the number to {wd}/csv_total.txt."
        ),
        "setup": lambda wd: (wd / "sales.csv").write_text("a,3\nb,4\nc,5\n"),
        "check": lambda wd, reply: (
            _norm(_read(wd, "csv_total.txt")) == "12",
            f"csv_total.txt={_norm(_read(wd, 'csv_total.txt'))!r}",
        ),
    },
    {
        "name": "tree_listing",
        "category": "shell",
        "prompt": lambda wd: (
            f"List all .txt files under {wd}/src (recursively) into "
            f"{wd}/listing.txt, one filename per line."
        ),
        "setup": lambda wd: [
            (wd / "src" / "a").mkdir(parents=True, exist_ok=True),
            (wd / "src" / "a" / "alpha.txt").write_text("a"),
            (wd / "src" / "b").mkdir(parents=True, exist_ok=True),
            (wd / "src" / "b" / "beta.txt").write_text("b"),
            (wd / "src" / "b" / "ignore.md").write_text("x"),
        ],
        "check": lambda wd, reply: (
            sorted(
                ln.strip().split("/")[-1]
                for ln in (_read(wd, "listing.txt") or "").splitlines()
                if ln.strip()
            )
            == ["alpha.txt", "beta.txt"],
            f"listing={_read(wd, 'listing.txt')!r}",
        ),
    },
    {
        "name": "multi_step_report",
        "category": "multi_step",
        "prompt": lambda wd: (
            f"Create a directory {wd}/report containing three files: "
            f"day1.txt, day2.txt, day3.txt, each holding the single line "
            f"'day N ok' (with N = 1, 2, 3 respectively). Then write "
            f"{wd}/report/summary.txt listing the three filenames, one per line."
        ),
        "setup": lambda wd: None,
        "check": lambda wd, reply: (
            all(
                _norm(_read(wd, f"report/day{i}.txt")) == f"day {i} ok"
                for i in (1, 2, 3)
            )
            and sorted(
                ln.strip()
                for ln in (_read(wd, "report/summary.txt") or "").splitlines()
                if ln.strip()
            )
            == ["day1.txt", "day2.txt", "day3.txt"],
            f"summary={_read(wd, 'report/summary.txt')!r}",
        ),
    },
]
