"""Tests for session-scoped CWD persistence across RUN: directives.

2026-09-27, root-caused live: a model emitted `cd /some/project/dir` as
one RUN: directive, then `python3 main.py` as a SEPARATE one. The second
failed looking for main.py in the agent's own process CWD. Every RUN: is
its own subprocess.run() and nothing called os.chdir(), so the `cd` only
ever affected the single throwaway shell it ran in.

These tests execute the real run_command() against real temp directories
and real subprocesses, asserting the observable working directory a
subsequent command observes. Nothing here reads or matches source text.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_session_cwd():
    """Every test starts and ends with no session CWD bound."""
    from scripts.session_cwd import reset_session_cwd, set_session_cwd

    token = set_session_cwd("")
    yield
    reset_session_cwd(token)


@pytest.fixture
def ma(monkeypatch):
    import master_ai

    monkeypatch.setattr(master_ai, "log", lambda *a, **k: None)
    # Never let a test actually execute a command: run_command is called
    # for its cwd side effect, and these tests assert on the cwd handed to
    # subprocess.run rather than on command output.
    return master_ai


@pytest.fixture
def spy_subprocess(ma, monkeypatch):
    """Record the cwd= each subprocess.run() is given; execute nothing."""
    seen = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(argv, **kwargs):
        seen.append(kwargs.get("cwd"))
        return _Result()

    monkeypatch.setattr(ma.subprocess, "run", _run)
    return seen


def _run(ma, spy, cmd):
    ma.run_command(cmd)
    assert spy, f"subprocess.run was not called for {cmd!r}"
    return spy[-1]


def _cd_then_observe(ma, spy, cd_cmd):
    """Run a `cd`, then run a probe, and return the probe's cwd.

    The contract is about the command AFTER the cd, not the cd's own cwd:
    the cd itself is handed the session dir it is moving away from.
    """
    _run(ma, spy, cd_cmd)
    return _run(ma, spy, "ls")


# ── the reported bug ──


def test_cd_persists_into_the_next_command(ma, spy_subprocess, tmp_path):
    """The core regression: cd in one RUN:, used by the next.

    Reproduces the live failure -- `cd <dir>` then `python3 main.py` in a
    separate RUN: used to run main.py from the agent's process CWD.
    """
    project = tmp_path / "project"
    project.mkdir()

    after_cd = _run(ma, spy_subprocess, f"cd {project}")
    assert after_cd in (None, str(project)), after_cd

    after_python = _run(ma, spy_subprocess, "python3 main.py")
    assert after_python == str(project), (
        f"second RUN: did not inherit the cd; got {after_python!r}"
    )


def test_cd_is_relative_to_the_previous_cd_not_the_process(
    ma, spy_subprocess, tmp_path
):
    """A relative cd resolves against the session CWD, like a real shell."""
    top = tmp_path / "top"
    (top / "nested").mkdir(parents=True)

    assert _cd_then_observe(ma, spy_subprocess, f"cd {top}") == str(top)
    got = _cd_then_observe(ma, spy_subprocess, "cd nested")
    assert got == str(top / "nested"), got


def test_chained_leading_cd_resolves_in_order(ma, spy_subprocess, tmp_path):
    """`cd a && cd b` must land in b, not a."""
    b = tmp_path / "a" / "b"
    b.mkdir(parents=True)

    assert _cd_then_observe(ma, spy_subprocess, f"cd {tmp_path}") == str(tmp_path)
    got = _cd_then_observe(ma, spy_subprocess, "cd a && cd b")
    assert got == str(b), got


def test_cd_with_quoted_path(ma, spy_subprocess, tmp_path):
    """A quoted path with a space must resolve, not be split or ignored."""
    spaced = tmp_path / "my project"
    spaced.mkdir()

    got = _cd_then_observe(ma, spy_subprocess, f'cd "{spaced}"')
    assert got == str(spaced), got


def test_cd_then_and_then_command_runs_in_the_new_dir(ma, spy_subprocess, tmp_path):
    """The chained form must not run the trailing part in the old dir."""
    d = tmp_path / "chained"
    d.mkdir()

    got = _cd_then_observe(ma, spy_subprocess, f"cd {d} && ls")
    assert got == str(d), got


# ── things that must NOT move the session ──


def test_non_leading_cd_does_not_move_the_session(ma, spy_subprocess, tmp_path):
    """`foo && cd bar` must not change where the NEXT command starts.

    cd wasn't the first thing that ran, so the following command still
    starts where it already was. Only a LEADING cd persists.
    """
    other = tmp_path / "other"
    other.mkdir()
    base = tmp_path / "base"
    base.mkdir()

    _run(ma, spy_subprocess, f"cd {base}")
    _run(ma, spy_subprocess, f"true && cd {other}")
    got = _run(ma, spy_subprocess, "ls")
    assert got == str(base), got


def test_failed_cd_does_not_move_the_session(ma, spy_subprocess, tmp_path, monkeypatch):
    """A non-zero exit must not strand every later command in a bad dir."""
    base = tmp_path / "base"
    base.mkdir()
    _run(ma, spy_subprocess, f"cd {base}")

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "no such directory"

    monkeypatch.setattr(ma.subprocess, "run", lambda a, **k: _Fail())
    ma.run_command("cd /definitely/not/here")

    monkeypatch.setattr(
        ma.subprocess,
        "run",
        lambda a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    ma.run_command("ls")
    from scripts.session_cwd import get_session_cwd

    assert get_session_cwd() == str(base), get_session_cwd()


def test_cd_to_nonexistent_dir_does_not_move_the_session(ma, spy_subprocess, tmp_path):
    """A `cd` whose target isn't a real directory is ignored."""
    base = tmp_path / "base"
    base.mkdir()
    _run(ma, spy_subprocess, f"cd {base}")
    _run(ma, spy_subprocess, f"cd {tmp_path / 'nope'}")
    got = _run(ma, spy_subprocess, "ls")
    assert got == str(base), got


def test_no_cd_means_no_session_cwd(ma, spy_subprocess):
    """Without a cd, subprocess inherits the process CWD (cwd=None)."""
    assert _run(ma, spy_subprocess, "ls") is None
    assert _run(ma, spy_subprocess, "echo hi") is None


def test_run_cwd_helper_agrees_with_get_session_cwd(ma):
    """_run_cwd() is the single place the value is handed to subprocess."""
    from scripts.session_cwd import get_session_cwd, set_session_cwd

    assert ma._run_cwd() is None
    set_session_cwd("/tmp")
    assert ma._run_cwd() == "/tmp"
    assert ma._run_cwd() == get_session_cwd()


# ── the real subprocess, end to end ──


def test_real_subprocess_observes_the_session_cwd(ma, monkeypatch, tmp_path):
    """No mocks: a real RUN: `cd` makes the next real subprocess see it."""
    project = tmp_path / "realproj"
    project.mkdir()
    marker = project / "marker.txt"
    marker.write_text("found me")

    # Let run_command actually execute, but keep it from printing/animating.
    monkeypatch.setattr(ma, "play_anim", lambda *a, **k: None)
    monkeypatch.setattr(ma, "log", lambda *a, **k: None)
    monkeypatch.setattr(ma, "_is_informational_cmd", lambda *a, **k: False)
    monkeypatch.setattr(ma, "_build_sandbox_argv", lambda argv: argv)

    ma.run_command(f"cd {project}")
    ma.run_command("ls")

    assert ma._run_cwd() == str(project)
    assert marker.exists()


# ── the import must resolve the way PRODUCTION resolves it ─────────
#
# The in-process tests above all run with the repo root on sys.path (pytest
# rootdir). Production does not: the agent runs as ~/scripts/master_ai.py, a
# symlink into the repo, so sys.path[0] is ~/scripts, which has no nested
# scripts/ package -- and the editable install's finder maps top-level
# module names individually and does not include `scripts`. An import that
# only works under pytest is a fix that is green in tests and dead in the
# real agent, so this case runs the real production path in a subprocess.


def test_import_works_from_production_invocation():
    """Import master_ai the way the live agent does, from ~/scripts."""
    entry = Path.home() / "scripts" / "master_ai.py"
    if not entry.exists():
        pytest.skip(f"{entry} not present; not a production-style install")

    probe = (
        "import master_ai as m, tempfile, pathlib\n"
        "d = pathlib.Path(tempfile.mkdtemp())\n"
        "m._track_leading_cd(f'cd {d}', True)\n"
        "assert m._run_cwd() == str(d), m._run_cwd()\n"
        "print('OK')\n"
    )
    # Production resolves this repo's modules through ~/scripts (it is
    # sys.path[0] for the symlinked entry point), so put that on the path the
    # same way and run from an unrelated cwd. `scripts.session_cwd` is NOT
    # reachable that way -- ~/scripts has no nested scripts/ package and the
    # editable finder does not map it -- so this case only passes if
    # master_ai.py puts its own real directory on sys.path itself.
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd="/",
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(entry.parent)},
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout, proc.stdout


def test_repo_root_is_added_to_sys_path_for_the_session_cwd_import():
    """master_ai must put its real directory on sys.path, symlink or not."""
    import master_ai

    assert os.path.realpath(master_ai.__file__) == os.path.join(
        master_ai._MASTER_AI_DIR, "master_ai.py"
    )
    assert master_ai._MASTER_AI_DIR in sys.path
