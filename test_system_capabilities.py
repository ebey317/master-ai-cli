"""Tests for the system-capability prompt block.

system_capability_scan.py probes the real machine — OS, arch, shell, desktop
session, which commands and package managers actually exist — and writes
~/.claf/system_capabilities.json. _inject_system_capabilities() reads that
and folds it into the system prompt, so the model describes this box rather
than a box it assumes.

That file was written on 2026-06-14 and read by nothing in master_ai until
this wiring landed: the scan ran, CLAF consumed it, and the agent guessed.

These use a temp CAP_FILE so they never read or clobber the real scan, and
assert the block is present in the prompt, cached, refreshable, and
non-fatal when the scan is unavailable.
"""

from __future__ import annotations

import json
import time

import pytest

CAPS = {
    "os": "Linux",
    "os_release": "6.1.0-test",
    "os_version": "test",
    "machine": "x86_64",
    "shell": "/bin/zsh",
    "desktop": {
        "xdg_current_desktop": "X-Cinnamon",
        "xdg_session_type": "x11",
        "desktop_session": "test",
        "display_server": "x11",
    },
    "terminal_emulators": ["gnome-terminal"],
    "package_managers": ["apt", "dpkg", "flatpak"],
    "common_commands": {"git": "2.43.0"},
    "distro": {"PRETTY_NAME": "TestOS 1.0"},
}


@pytest.fixture
def scan(tmp_path, monkeypatch):
    """system_capability_scan pointed at a temp capability file."""
    import system_capability_scan as s

    cap = tmp_path / "system_capabilities.json"
    cap.write_text(json.dumps(CAPS))
    monkeypatch.setattr(s, "CAP_FILE", cap)
    return s


@pytest.fixture
def ma(monkeypatch):
    import master_ai

    monkeypatch.setattr(master_ai, "log", lambda *a, **k: None)
    master_ai._SYS_CAP_CACHE["ts"] = 0.0
    master_ai._SYS_CAP_CACHE["block"] = ""
    return master_ai


# ── the block reaches the prompt ──


def test_block_reports_the_real_platform(scan, ma):
    block = ma._inject_system_capabilities()
    assert "Linux" in block
    assert "x86_64" in block
    assert "X-Cinnamon" in block
    assert "apt" in block


def test_identity_injection_includes_the_capability_block(scan, ma):
    """The agent must actually be told, not merely able to look it up."""
    msgs = ma._inject_identity([{"role": "user", "content": "hi"}])
    head = msgs[0]["content"]
    assert "THIS MACHINE" in head
    assert "X-Cinnamon" in head


def test_capability_block_does_not_replace_the_identity(scan, ma):
    """Both halves must survive, and identity must come first."""
    msgs = ma._inject_identity([{"role": "user", "content": "hi"}])
    head = msgs[0]["content"]
    assert head.startswith(ma.MASTER_AI_IDENTITY_SYSTEM[:40])
    assert "THIS MACHINE" in head


def test_existing_system_content_is_still_merged(ma, monkeypatch):
    """A caller-supplied system message must survive the injection."""
    # Via monkeypatch, not bare assignment: replacing the function outright
    # leaks the stub into every later test in the session.
    monkeypatch.setattr(ma, "_inject_system_capabilities", lambda force=False: "CAPS")
    msgs = ma._inject_identity(
        [
            {"role": "system", "content": "caller rules"},
            {"role": "user", "content": "x"},
        ]
    )
    assert "caller rules" in msgs[0]["content"]
    assert "CAPS" in msgs[0]["content"]


# ── caching ──


def test_block_is_cached_between_calls(scan, ma, monkeypatch):
    first = ma._inject_system_capabilities()
    calls = []
    import system_capability_scan as s

    monkeypatch.setattr(
        s, "format_prompt_block", lambda caps: calls.append(1) or "SECOND"
    )
    second = ma._inject_system_capabilities()
    assert calls == [], "cache was not used inside the TTL"
    assert second == first


def test_force_bypasses_the_cache(scan, ma, monkeypatch):
    ma._inject_system_capabilities()
    import system_capability_scan as s

    monkeypatch.setattr(s, "format_prompt_block", lambda caps: "REFRESHED")
    assert ma._inject_system_capabilities(force=True) == "REFRESHED"


def test_expired_cache_is_rebuilt(scan, ma, monkeypatch):
    ma._inject_system_capabilities()
    ma._SYS_CAP_CACHE["ts"] = time.time() - (ma._SYS_CAP_TTL_S + 1)
    import system_capability_scan as s

    monkeypatch.setattr(s, "format_prompt_block", lambda caps: "AFTER_TTL")
    assert ma._inject_system_capabilities() == "AFTER_TTL"


# ── failure must be non-fatal ──


def test_missing_capability_file_does_not_raise(ma, monkeypatch, tmp_path):
    import system_capability_scan as s

    monkeypatch.setattr(s, "CAP_FILE", tmp_path / "absent.json")
    # load() falls back to discover(); make that fail too.
    monkeypatch.setattr(
        s, "discover", lambda: (_ for _ in ()).throw(OSError("probe failed"))
    )
    assert ma._inject_system_capabilities(force=True) == ""


def test_scan_module_unavailable_does_not_raise(ma, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "system_capability_scan":
            raise ImportError("no module")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert ma._inject_system_capabilities(force=True) == ""


def test_a_failed_refresh_keeps_the_previous_block(ma, monkeypatch, scan):
    """Losing the scan must not silently strip capability from the prompt."""
    good = ma._inject_system_capabilities()
    assert good
    import system_capability_scan as s

    monkeypatch.setattr(
        s, "format_prompt_block", lambda caps: (_ for _ in ()).throw(OSError("boom"))
    )
    assert ma._inject_system_capabilities(force=True) == good


# ── the syscap command ──


def test_syscap_show_prints_the_block(scan, ma, capsys):
    assert ma._handle_syscap_cmd("syscap", "syscap") is True
    assert "X-Cinnamon" in capsys.readouterr().out


def test_syscap_refresh_rescans_and_forces_the_cache(scan, ma, capsys, monkeypatch):
    ran = []
    monkeypatch.setattr(scan, "discover", lambda: ran.append(1) or CAPS)
    monkeypatch.setattr(scan, "save", lambda caps: None)

    assert ma._handle_syscap_cmd("syscap refresh", "syscap refresh") is True
    assert ran == [1], "refresh did not re-probe"
    assert "re-scanned" in capsys.readouterr().out


def test_syscap_ignores_other_commands(ma):
    for lo in ("doctor", "help", "syscaps", "sys"):
        assert ma._handle_syscap_cmd(lo, lo) is False
