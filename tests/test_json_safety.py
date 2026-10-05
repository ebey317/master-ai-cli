"""Tests for JSON safety utilities."""

import json
import tempfile
from pathlib import Path

import pytest

from scripts.json_safety import (
    load_json_dict,
    load_json_lines_dicts,
    try_parse_json_dict,
    scan_json_files,
    safe_get,
    safe_get_nested,
    NonDictJSONError,
)


class TestLoadJsonDict:
    def test_valid_dict(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"key": "value", "num": 42}, f)
            path = Path(f.name)
        try:
            result = load_json_dict(path)
            assert result == {"key": "value", "num": 42}
        finally:
            path.unlink()

    def test_non_dict_json_number(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("42")
            path = Path(f.name)
        try:
            result = load_json_dict(path)
            assert result is None
        finally:
            path.unlink()

    def test_non_dict_json_string(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('"oops"')
            path = Path(f.name)
        try:
            result = load_json_dict(path)
            assert result is None
        finally:
            path.unlink()

    def test_non_dict_json_array(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("[1, 2, 3]")
            path = Path(f.name)
        try:
            result = load_json_dict(path)
            assert result is None
        finally:
            path.unlink()

    def test_non_dict_json_null(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("null")
            path = Path(f.name)
        try:
            result = load_json_dict(path)
            assert result is None
        finally:
            path.unlink()

    def test_invalid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{ not valid json }")
            path = Path(f.name)
        try:
            result = load_json_dict(path)
            assert result is None
        finally:
            path.unlink()

    def test_missing_file(self):
        result = load_json_dict(Path("/nonexistent/file.json"))
        assert result is None


class TestLoadJsonLinesDicts:
    def test_mixed_valid_and_invalid_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"valid": true}\n')
            f.write("42\n")
            f.write('{"also": "valid"}\n')
            f.write('not json\n')
            f.write('["array"]\n')
            f.write('{"final": 1}\n')
            path = Path(f.name)
        try:
            results = load_json_lines_dicts(path)
            assert len(results) == 3
            assert results[0] == {"valid": True}
            assert results[1] == {"also": "valid"}
            assert results[2] == {"final": 1}
        finally:
            path.unlink()

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
        try:
            results = load_json_lines_dicts(path)
            assert results == []
        finally:
            path.unlink()


class TestTryParseJsonDict:
    def test_valid_dict(self):
        result = try_parse_json_dict('{"a": 1}')
        assert result == {"a": 1}

    def test_non_dict_values(self):
        assert try_parse_json_dict("42") is None
        assert try_parse_json_dict('"str"') is None
        assert try_parse_json_dict("[1,2]") is None
        assert try_parse_json_dict("null") is None
        assert try_parse_json_dict("true") is None

    def test_invalid_json(self):
        assert try_parse_json_dict("{") is None


class TestScanJsonFiles:
    def test_scans_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dir_path = Path(tmpdir)
            (dir_path / "a.json").write_text('{"name": "a"}')
            (dir_path / "b.json").write_text("42")  # non-dict
            (dir_path / "c.json").write_text('{"name": "c"}')
            (dir_path / "d.txt").write_text("not json")

            results = scan_json_files(dir_path, "*.json")
            assert len(results) == 2
            names = [data["name"] for _, data in results]
            assert set(names) == {"a", "c"}

    def test_with_validator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dir_path = Path(tmpdir)
            (dir_path / "a.json").write_text('{"name": "a", "enabled": true}')
            (dir_path / "b.json").write_text('{"name": "b", "enabled": false}')

            results = scan_json_files(dir_path, "*.json", validator=lambda d: d.get("enabled"))
            assert len(results) == 1
            assert results[0][1]["name"] == "a"


class TestSafeGet:
    def test_dict_returns_value(self):
        assert safe_get({"a": 1}, "a") == 1
        assert safe_get({"a": 1}, "b", "default") == "default"

    def test_non_dict_returns_default(self):
        assert safe_get("not a dict", "a", "default") == "default"
        assert safe_get([1, 2, 3], "a", "default") == "default"
        assert safe_get(42, "a", "default") == "default"
        assert safe_get(None, "a", "default") == "default"


class TestSafeGetNested:
    def test_valid_nested_dict(self):
        data = {"a": {"b": {"c": 42}}}
        assert safe_get_nested(data, "a", "b", "c") == 42

    def test_missing_key_returns_default(self):
        data = {"a": {"b": {}}}
        assert safe_get_nested(data, "a", "b", "c", default="missing") == "missing"

    def test_non_dict_intermediate_returns_default(self):
        data = {"a": "not a dict"}
        assert safe_get_nested(data, "a", "b", default="oops") == "oops"

    def test_non_dict_root_returns_default(self):
        assert safe_get_nested("not a dict", "a", default="oops") == "oops"
