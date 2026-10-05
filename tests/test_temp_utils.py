"""Tests for portable temp utilities."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
import pytest

from scripts.temp_utils import get_sensei_tempdir, make_sensei_tempdir, sensei_temp_path


def test_default_resolves_to_platform_temp(monkeypatch: pytest.MonkeyPatch) -> None:
    # Strip all known env vars so we fall back to tempfile.gettempdir()
    for var in ("SENSEI_TMPDIR", "TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)
    expected = Path(tempfile.gettempdir()).resolve()
    assert get_sensei_tempdir() == expected


def test_explicit_sensei_tmpdir_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "my-sensei-tmp"
    monkeypatch.setenv("SENSEI_TMPDIR", str(custom))
    assert get_sensei_tempdir() == custom.resolve()


def test_make_unique_dir_creates_under_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENSEI_TMPDIR", str(tmp_path))
    d = make_sensei_tempdir("test-")
    assert d.exists()
    assert d.parent == tmp_path.resolve()
    assert d.name.startswith("test-")


def test_make_fixed_subdir_reuses_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENSEI_TMPDIR", str(tmp_path))
    d1 = make_sensei_tempdir(subdir="cache")
    d2 = make_sensei_tempdir(subdir="cache")
    assert d1 == d2 == tmp_path.resolve() / "cache"
    assert d1.exists()


def test_sensei_temp_path_creates_parents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENSEI_TMPDIR", str(tmp_path))
    p = sensei_temp_path("a", "b", "c.txt", mkdir=True)
    assert p.parent.exists()
    assert p == tmp_path.resolve() / "a" / "b" / "c.txt"
