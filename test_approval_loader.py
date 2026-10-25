"""Loader test: approval_queue._load_handlers must register the
suggestive-thinking consumer's handlers from the canonical
SCRIPTS_DIR/suggestive_thinking_sources.py file — by explicit path, not
__import__-by-name (the script-mode resolution that silently registered
nothing on 2026-10-23 while module-mode probes passed; an editable-install
finder + stale-name resolution can serve a different copy depending on how
the process started — the explicit path ends that).

Runs against a tmp SCRIPTS_DIR; never touches ~/scripts, and every
interpreter-global mutation (sys.path, sys.modules) is snapshotted and
restored — these tests must not pollute the rest of the suite.
"""

from __future__ import annotations

import sys
import types

import approval_queue


def _make_consumer_file(path, kind, handler_name="run_ok"):
    consumer = f'''
import approval_queue as _aq  # the importing alias — registrations land here

@_aq.register_handler("{kind}")
def {handler_name}(entry):
    return "ran: " + entry.get("payload", {{}}).get("marker", "")
'''
    path.write_text(consumer)


class _RealStateGuard:
    """Full snapshot/restore of the interpreter state the loader touches:
    sys.path contents AND whether/what sys.modules holds under the
    consumer's name. Pop-after-patch is not restoration — the next import
    re-resolves against whatever sys.path junk we left behind."""

    def __init__(self, name: str = "suggestive_thinking_sources") -> None:
        self.name = name
        self.saved_path: list[str] = []
        self.saved_module: types.ModuleType | None = None
        self.had_module = False

    def __enter__(self) -> _RealStateGuard:
        self.saved_path = list(sys.path)
        self.had_module = self.name in sys.modules
        saved = sys.modules.get(self.name)
        self.saved_module = saved if isinstance(saved, types.ModuleType) else None
        return self

    def __exit__(self, *exc: object) -> None:
        sys.path[:] = self.saved_path
        if self.had_module and self.saved_module is not None:
            sys.modules[self.name] = self.saved_module
        else:
            sys.modules.pop(self.name, None)


def test_loader_registers_suggestive_handlers_from_explicit_path(
    tmp_path, monkeypatch, capsys
):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    consumer = scripts_dir / "suggestive_thinking_sources.py"
    _make_consumer_file(consumer, "grant_packet_probe")

    with _RealStateGuard():
        monkeypatch.setattr(approval_queue, "SCRIPTS_DIR", scripts_dir)
        approval_queue._load_handlers()
        assert "grant_packet_probe" in approval_queue._HANDLERS

    monkeypatch.undo()
    out = capsys.readouterr().err
    assert "handlers loaded from" in out  # the load is never silent from now on


def test_loader_missing_consumer_reports_hint_not_crash(tmp_path, monkeypatch, capsys):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()  # no consumer file at all

    with _RealStateGuard():
        monkeypatch.setattr(approval_queue, "SCRIPTS_DIR", scripts_dir)
        approval_queue._load_handlers()

    monkeypatch.undo()
    out = capsys.readouterr().err
    assert "missing" in out  # hint, visibility not silence
    # missing consumers never block the CLI: the two legacy __import__
    # consumers warn loudly if unimportable, and execution continues


def test_loader_stale_module_resolution_is_replaced(tmp_path, monkeypatch, capsys):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    consumer = scripts_dir / "suggestive_thinking_sources.py"
    _make_consumer_file(consumer, "grant_packet_probe_2", handler_name="run_ok2")

    with _RealStateGuard():
        # Plant a STALE fake module under the same name — the loader must
        # evict it and load the file from the canonical path instead.
        stale = types.ModuleType("suggestive_thinking_sources")
        stale.__file__ = str(
            tmp_path / "stale" / "wrong" / "suggestive_thinking_sources.py"
        )
        stale.marker = "stale"
        sys.modules["suggestive_thinking_sources"] = stale
        monkeypatch.setattr(approval_queue, "SCRIPTS_DIR", scripts_dir)

        approval_queue._load_handlers()
        loaded = sys.modules["suggestive_thinking_sources"]
        assert getattr(loaded, "marker", None) != "stale"  # stale copy evicted
        assert "grant_packet_probe_2" in approval_queue._HANDLERS
