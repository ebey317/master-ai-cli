"""Tests for setup/uninstall/install CLI help paths."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_WIZARD = REPO_ROOT / "setup_wizard.py"
UNINSTALL_WIZARD = REPO_ROOT / "uninstall_wizard.py"
INSTALL_SH = REPO_ROOT / "install.sh"


def _home_files(home: Path) -> list[str]:
    """Return paths under home that look like Master AI state."""
    out = []
    for p in home.rglob("*"):
        rel = p.relative_to(home).as_posix()
        if p.is_file() and (
            ".master_ai" in rel
            or "scripts" in rel.split("/")[:1]
            or ".local/bin" in rel
        ):
            out.append(rel)
    return out


def test_setup_wizard_help_exits_cleanly(tmp_path):
    """`python3 setup_wizard.py --help` prints usage and writes nothing."""
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    result = subprocess.run(
        [sys.executable, str(SETUP_WIZARD), "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "master-ai --setup" in result.stdout
    assert "API keys" in result.stdout
    assert "MCLI_SKIP_SETUP" in result.stdout
    assert _home_files(home) == [], f"unexpected files created: {_home_files(home)}"


def test_uninstall_wizard_help_exits_cleanly(tmp_path):
    """`python3 uninstall_wizard.py --help` prints usage, no menu/clear/cancel."""
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    result = subprocess.run(
        [sys.executable, str(UNINSTALL_WIZARD), "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "master-ai --uninstall" in result.stdout
    assert "total wipe" in result.stdout.lower()
    assert "Cancelled" not in result.stdout
    assert "Choice" not in result.stdout
    assert "\033[" not in result.stdout, "--help should not clear the screen"
    assert _home_files(home) == [], f"unexpected files created: {_home_files(home)}"


def test_install_sh_help_exits_cleanly(tmp_path):
    """`bash install.sh --help` prints usage before any side effect."""
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "Usage: bash install.sh" in result.stdout
    assert "no dry-run" in result.stdout
    assert "~/scripts" in result.stdout
    assert _home_files(home) == [], f"unexpected files created: {_home_files(home)}"


def test_install_sh_key_helper_accepts_cerebras_and_fireworks(tmp_path):
    """install.sh's shared helper must save csk- and fw_ keys to ~/.master_ai_keys."""
    home = tmp_path / "home"
    home.mkdir()
    keys_file = home / ".master_ai_keys"

    # Extract the bash helper function from install.sh so we can run it
    # without executing the rest of the installer.
    text = INSTALL_SH.read_text()
    start = text.find("_save_detected_key() {")
    assert start != -1, "_save_detected_key helper not found in install.sh"
    end = text.find("\n}\n", start)
    assert end != -1, "could not find end of _save_detected_key helper"
    helper = text[start : end + 3]

    helper_file = tmp_path / "helper.sh"
    helper_file.write_text(helper)

    env = {**os.environ, "HOME": str(home)}
    script = f'source "{helper_file}"; _save_detected_key "csk-abcdefghijklmnopqrstuvwxyz1234567890"; _save_detected_key "fw_abcdefghijklmnopqrstuvwxyz1234567890"'
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "cerebras: saved as PRIMARY" in result.stdout
    assert "fireworks: saved as PRIMARY" in result.stdout

    data = json.loads(keys_file.read_text())
    assert data["cerebras"] == "csk-abcdefghijklmnopqrstuvwxyz1234567890"
    assert data["fireworks"] == "fw_abcdefghijklmnopqrstuvwxyz1234567890"


def test_install_sh_no_longer_has_inline_key_prefix_maps():
    """The three inline Python prefix maps should have been replaced."""
    text = INSTALL_SH.read_text()
    assert "_save_detected_key" in text
    assert "from setup_wizard import _detect_provider" in text
    # The old repeated deepseek line should be gone entirely.
    assert 'elif key.startswith("sk-"):      field = "deepseek"' not in text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
