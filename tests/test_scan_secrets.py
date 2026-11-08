"""Exercise scanner against isolated temporary Git repositories."""

import subprocess
import sys
from pathlib import Path

SCANNER = Path(__file__).resolve().parents[1] / "scripts" / "scan_secrets.py"


def run_scan(tmp_path, contents):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "sample.txt").write_text(contents)
    subprocess.run(["git", "add", "sample.txt"], cwd=tmp_path, check=True)
    return subprocess.run(
        [sys.executable, str(SCANNER)], cwd=tmp_path, text=True, capture_output=True
    )


def test_scanner_finds_secret_without_printing_value(tmp_path):
    secret = "gsk_" + "abcdef0123456789" * 3
    result = run_scan(tmp_path, f"credential = {secret}\n")
    assert result.returncode == 1
    assert "sample.txt:1: GROQ" in result.stdout
    assert secret not in result.stdout + result.stderr


def test_scanner_ignores_placeholder(tmp_path):
    result = run_scan(tmp_path, 'API_KEY="your_key_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"\n')
    assert result.returncode == 0


def test_scanner_clean_repo(tmp_path):
    result = run_scan(tmp_path, "ordinary text\n")
    assert result.returncode == 0
    assert "0 findings" in result.stdout
