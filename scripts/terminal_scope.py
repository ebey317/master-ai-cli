"""Read TERMINAL_ENV / TERMINAL_CWD through the active profile scope."""

from __future__ import annotations

import os
from pathlib import Path

from . import profile_scope


def terminal_env() -> dict[str, str]:
    return profile_scope.scoped_env()


def terminal_cwd() -> Path:
    return profile_scope.get_terminal_cwd()


def apply_to_process() -> None:
    """For tools that still require mutation of the process, snapshot the scope.

    In a multiplexed agent this must only be called in a routed turn's executor,
    never at module import, to avoid poisoning other profiles.
    """
    env = terminal_env()
    os.environ.update(env)
    os.environ["SENSEI_CWD"] = str(terminal_cwd())
    os.chdir(terminal_cwd())
