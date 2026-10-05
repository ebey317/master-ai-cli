"""
Safe JSON loading utilities for Sensei scripts and tools.

Portable design from upstream: JSON.parse succeeds on non-object values (numbers,
strings, arrays, null). Callers that assume a dict and call .get()/.keys() will
raise AttributeError/TypeError. This module provides helpers that validate the
parsed value is a dict and apply a consistent "corrupt file" policy.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class NonDictJSONError(ValueError):
    """Raised when parsed JSON is not a dict/object."""
    pass


def load_json_dict(path: Path, encoding: str = "utf-8") -> dict | None:
    """
    Load a JSON file and return the parsed dict, or None if invalid.

    Policy: treat non-dict JSON, decode errors, and OS errors as corrupt.
    Logs a warning and returns None.
    """
    try:
        data = json.loads(path.read_text(encoding=encoding))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Unreadable JSON file %s: %s", path, e)
        return None

    if not isinstance(data, dict):
        log.warning("Non-dict JSON in %s (got %s), treating as corrupt", path, type(data).__name__)
        return None

    return data


def load_json_lines_dicts(path: Path, encoding: str = "utf-8") -> list[dict]:
    """
    Load a JSONL file, keeping only valid dict objects per line.

    Policy: skip lines that fail to parse or parse to non-dict values.
    Returns list of dicts (may be empty).
    """
    results: list[dict] = []
    try:
        text = path.read_text(encoding=encoding)
    except OSError as e:
        log.warning("Cannot read JSONL file %s: %s", path, e)
        return results

    for line_num, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            log.warning("Invalid JSON on line %d of %s: %s", line_num, path, e)
            continue

        if not isinstance(entry, dict):
            log.warning("Non-dict JSON on line %d of %s (got %s), skipping",
                        line_num, path, type(entry).__name__)
            continue

        results.append(entry)

    return results


def try_parse_json_dict(text: str) -> dict | None:
    """
    Parse a JSON string and return dict if valid, else None.

    Policy: non-dict JSON and decode errors return None (no logging).
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def scan_json_files(
    directory: Path,
    pattern: str = "*.json",
    validator: Callable[[dict], bool] | None = None,
) -> list[tuple[Path, dict]]:
    """
    Scan a directory for JSON files, load each as dict, optionally validate.

    Returns list of (path, dict) for files that parse as dict and pass validator.
    Corrupt/unparseable/non-dict files are skipped with warnings.
    """
    results: list[tuple[Path, dict]] = []
    for path in directory.glob(pattern):
        data = load_json_dict(path)
        if data is None:
            continue
        if validator is not None and not validator(data):
            log.debug("File %s failed validator, skipping", path)
            continue
        results.append((path, data))
    return results


def safe_get(data: Any, key: str, default: T = None) -> T | Any:
    """
    Safely get a key from data if it's a dict, else return default.

    Prevents AttributeError when data is not a dict.
    """
    if isinstance(data, dict):
        return data.get(key, default)
    return default


def safe_get_nested(data: Any, *keys: str, default: T = None) -> T | Any:
    """
    Safely traverse nested dicts. Returns default if any level is not a dict.
    """
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current
