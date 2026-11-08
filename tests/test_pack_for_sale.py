"""Tests for pack_for_sale.sh secret scrubber and buyer packaging helpers."""

import re
import subprocess
from pathlib import Path

import pytest

PACK_SCRIPT = Path(__file__).resolve().parents[1] / "pack_for_sale.sh"


def _extract_key_re() -> str:
    """Pull the secret regex out of pack_for_sale.sh."""
    text = PACK_SCRIPT.read_text()
    m = re.search(r"_KEY_RE='([^']+)'", text)
    if not m:
        raise RuntimeError("could not find _KEY_RE in pack_for_sale.sh")
    return m.group(1)


def test_scrubber_removes_cerebras_and_fireworks_keys(tmp_path):
    """The scrub regex must delete lines containing fake csk-/fw_ keys."""
    key_re = _extract_key_re()
    staged = tmp_path / "staged"
    staged.mkdir()
    dirty = staged / "dummy.py"
    dirty.write_text(
        "# config\n"
        "cerebras = 'csk-abcdefghijklmnopqrstuvwxyz1234567890'\n"
        "fireworks = 'fw_abcdefghijklmnopqrstuvwxyz1234567890'\n"
        "# end\n"
    )

    # Replicate the scrub pass from pack_for_sale.sh
    grep_proc = subprocess.run(
        [
            "find",
            str(staged),
            "-type",
            "f",
            "(",
            "-name",
            "*.py",
            "-o",
            "-name",
            "*.sh",
            "-o",
            "-name",
            "*.md",
            "-o",
            "-name",
            "*.json",
            "-o",
            "-name",
            "*.yaml",
            "-o",
            "-name",
            "*.yml",
            "-o",
            "-name",
            "*.txt",
            ")",
        ],
        capture_output=True,
        text=True,
    )
    matched = [p for p in grep_proc.stdout.splitlines() if p.strip()]
    assert str(dirty) in matched, "dirty file should match the secret regex"

    for p in matched:
        subprocess.run(["sed", "-i", "-E", f"/{key_re}/d", p], check=True)

    result = dirty.read_text()
    assert "csk-abcdefghijklmnopqrstuvwxyz1234567890" not in result
    assert "fw_abcdefghijklmnopqrstuvwxyz1234567890" not in result
    assert "cerebras =" not in result
    assert "fireworks =" not in result
    assert "# config" in result
    assert "# end" in result


def test_regex_matches_cerebras_and_fireworks():
    """Unit-level sanity check on the extracted regex itself."""
    key_re = _extract_key_re()
    assert re.search(key_re, "csk-abcdefghijklmnopqrstuvwxyz1234567890")
    assert re.search(key_re, "fw_abcdefghijklmnopqrstuvwxyz1234567890")
    assert not re.search(key_re, "csk-short")
    assert not re.search(key_re, "fw_short")
    # Prompt strings with bare prefix (no long body) must not match.
    assert not re.search(key_re, "Paste your sk-or-v1- key here")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
