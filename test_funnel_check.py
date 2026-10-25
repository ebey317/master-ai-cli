"""Tests for funnel_check.py — the read-only money-funnel surface the
MONEY/ORG items run ("what's the distance to live/sale", with receipts).
All state comes from tmp files; never the real key, never the network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import funnel_check


def _products_stub():
    return {
        "success": True,
        "products": [
            {
                "name": "AI Controller — Linux",
                "price": 3000,
                "published": False,
                "sales_count": 0,
                "custom_permalink": "ai-controller-linux",
                "attached_files": [],
            },
            {
                "name": "BIOVEGA",
                "price": 10000,
                "published": True,
                "sales_count": 2,
                "custom_permalink": "biovega-vol-1",
                "attached_files": [{"name": "book.pdf"}],
            },
        ],
    }


def test_gumroad_funnel_counts_light_dark_and_attached(tmp_path):
    jf = tmp_path / "products.json"
    jf.write_text(json.dumps(_products_stub()))
    r = subprocess.run(
        [
            sys.executable,
            str(Path(funnel_check.__file__)),
            "gumroad",
            "--json-file",
            str(jf),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0
    assert (
        "store: 2 product(s), 1 published, 1 with deliverable files, 2 total sales"
        in out
    )
    assert "NO FILE ATTACHED" in out  # the dark one says so plainly
    assert "one attach + one publish flip" in out


def test_gumroad_funnel_missing_products_file_exits():
    r = subprocess.run(
        [
            sys.executable,
            str(Path(funnel_check.__file__)),
            "gumroad",
            "--json-file",
            "/nope/none.json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 1  # honest failure, never a fake table
    assert "store:" not in r.stdout  # no fake summary printed
    assert "Traceback" not in (r.stdout + r.stderr)  # clean failure, no crash
    assert (
        "not reporting fake numbers" in (r.stdout + r.stderr)
    )  # honest reason given


def test_no_key_lines_the_honest_reason_not_a_fake_funnel(
    monkeypatch, tmp_path, capsys
):
    stub = tmp_path / "keys"
    stub.write_text("OTHER_KEY=1\n")
    monkeypatch.setattr(funnel_check, "KEYS_FILE", stub)
    monkeypatch.setattr(funnel_check, "_load_key", lambda: None)
    ok = funnel_check._gumroad_products()
    assert ok is None
    assert "no GUMROAD_API_KEY" in capsys.readouterr().out


def test_ai_controller_funnel_reports_distance_with_receipts(tmp_path):
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    (repo / ".gitref").write_text("x")  # presence marker
    env = dict(os.environ, FUNNEL_KEYS_FILE=str(tmp_path / "missing-keys"))
    r = subprocess.run(
        [
            sys.executable,
            str(Path(funnel_check.__file__)),
            "ai-controller",
            "--repo",
            str(repo),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    out = r.stdout + r.stderr
    # untagged repo: honest report of the first flip, no crash
    assert "NO release tag" in out
    assert "NO release assets" in out
