#!/usr/bin/env python3
"""The calling master_ai module instance, for the current call stack.

Before the 2026-10-05 monolith split (HANDOFF.md), handle()/process_reply()/
compact_history() etc. all lived directly inside master_ai.py, so they
always ran against their OWN module's globals (MODE, PINNED_MODEL, the
approval queue, ...) automatically -- Python's `global` statement inside a
function always binds to that function's own `__module__`.

Splitting them into orchestration.py/dispatch.py/context.py broke that:
those modules now need to reach back into "whichever master_ai instance
called me" to read/write the same globals, and the pattern each used was
`sys.modules["master_ai"]` or `sys.modules.get("master_ai")` -- hardcoding
the literal module name.

That hardcoding is incompatible with stt_server.py's (Pupil's backend)
per-lane isolation: it deliberately loads an independent copy of
master_ai.py per routing lane (local/cloud_fast/cloud_deep/...) under a
DIFFERENT module name each time specifically so one slow cloud lane can
never block another (see stt_server.py's _load_api_master_ai_module and the
comment above _API_HANDLE_LANES explaining why). None of those lane copies
is ever registered under the literal key "master_ai", so the hardcoded
lookup returned None for any Pupil request touching the new split code --
reproduced live as `/chat` returning HTTP 500, `'NoneType' object has no
attribute 'BC'` (2026-10-06).

Fix: the caller (master_ai.py's own thin wrapper, which always correctly
knows its OWN module via `sys.modules[__name__]`) sets the caller module
here for the duration of its call; orchestration.py/dispatch.py/context.py
read it back instead of guessing a hardcoded name. Thread-local because
stt_server.py runs concurrent lanes on separate threads (the whole point of
per-lane isolation is that they must never block each other, so they must
also never see each other's "current caller").

Falls back to `sys.modules.get("master_ai")` when nothing was set -- the
common case for every caller that is not lane-isolated (plain `import
master_ai`, the interactive TUI, `python3 -c "import master_ai"`, tests).
"""

from __future__ import annotations

import functools
import sys
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

_local = threading.local()


def get() -> Any:
    """The current call stack's master_ai-like module, or None if there
    truly is none (no caller set one, and sys.modules has no "master_ai")."""
    override = getattr(_local, "module", None)
    if override is not None:
        return override
    return sys.modules.get("master_ai")


@contextmanager
def using(module: Optional[Any]) -> Iterator[None]:
    """Make *module* the answer to get() for the duration of this call
    stack (this thread only). A caller that passes None is a no-op --
    get() falls through to its normal default. Restores whatever was
    set before on exit, so nested calls (process_reply calling back into
    orchestrate, etc.) within the same thread compose correctly."""
    if module is None:
        yield
        return
    previous = getattr(_local, "module", None)
    _local.module = module
    try:
        yield
    finally:
        _local.module = previous


def bound(module: Any) -> Callable[[Callable], Callable]:
    """Decorator: every call to the wrapped function runs under
    `using(module)`. *module* is evaluated once, at decoration time --
    apply this directly to master_ai.py's thin delegate wrappers with
    `@runtime_host.bound(sys.modules[__name__])`, so each lane-isolated
    copy of master_ai.py (loaded under its own module name -- see this
    module's docstring) decorates its OWN wrappers with ITS OWN module,
    not whichever one happened to be "master_ai" at the time."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with using(module):
                return fn(*args, **kwargs)

        return wrapper

    return decorator
