#!/usr/bin/env python3
"""Scan tracked text files for likely secrets without printing their values."""

import hashlib
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "OPENROUTER": re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{20,}"),
    "SK_KEY": re.compile(r"\bsk-(?!or-v1-)[A-Za-z0-9_-]{20,}"),
    "GROQ": re.compile(r"\bgsk_[A-Za-z0-9_-]{20,}"),
    "NVIDIA": re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}"),
    "AWS": re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    "GITHUB": re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    "SLACK": re.compile(r"\bxox[bp]-[A-Za-z0-9-]{20,}"),
    "PRIVATE_KEY": re.compile(r"-----BEGIN (?:RSA|OPENSSH|EC) PRIVATE KEY-----"),
    "ASSIGNMENT": re.compile(
        r"(?i)\b[A-Za-z_][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Za-z0-9_]*\s*[:=]\s*['\"]([A-Za-z0-9+/=_-]{32,})['\"]"
    ),
}
PLACEHOLDER = re.compile(
    r"(?i)(?:your[_-]?key|example|placeholder|redacted|xxxxxxxx|<[^>]+>)"
)
FIXTURES = {
    (
        "test_hooks.py",
        167,
        "PRIVATE_KEY",
        "03d104c669e3c7b6be7f989db8b12c8b910d3be8c1e2a73c9369d3cc0ba803b5",
    ),
    (
        "test_hooks.py",
        173,
        "GITHUB",
        "adab3d665bcc2b445ec0da7e239d89dc7b2a88e9ddcf464f1ee8e641496deb89",
    ),
    (
        "test_privacy_cloud_guard.py",
        91,
        "AWS",
        "457643f44d19aed85fd756aa50cc0cd6b57376d4e8f5a72f9f85972a522002a3",
    ),
    (
        "test_privacy_cloud_guard.py",
        230,
        "AWS",
        "457643f44d19aed85fd756aa50cc0cd6b57376d4e8f5a72f9f85972a522002a3",
    ),
}


def scan(root):
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
    findings = []
    count = 0
    for item in files:
        if not item:
            continue
        path = Path(root) / item.decode("utf-8", "surrogateescape")
        try:
            if not path.is_file() or path.stat().st_size > 1_000_000:
                continue
            data = path.read_bytes()
            if b"\0" in data:
                continue
            text = data.decode("utf-8")
        except (OSError, UnicodeError):
            continue
        count += 1
        for number, line in enumerate(text.splitlines(), 1):
            for name, pattern in PATTERNS.items():
                for match in pattern.finditer(line):
                    value = match.group(0)
                    identity = (
                        str(path.relative_to(root)),
                        number,
                        name,
                        hashlib.sha256(value.encode()).hexdigest(),
                    )
                    if PLACEHOLDER.search(value) or identity in FIXTURES:
                        continue
                    findings.append((str(path.relative_to(root)), number, name))
    return count, sorted(set(findings))


def main():
    root = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
    )
    count, findings = scan(root)
    for path, line, name in findings:
        print(f"{path}:{line}: {name}")
    print(f"Scanned {count} files; {len(findings)} findings")
    return bool(findings)


if __name__ == "__main__":
    sys.exit(main())
