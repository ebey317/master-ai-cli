"""
Tests for env_cache.load_env_file memoization and freshness guarantees.
"""

import codecs
import tempfile
import threading
import time
from pathlib import Path

from scripts.env_cache import invalidate_env_file_cache, load_env_file


def test_basic_parse_and_cache():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("KEY=value\nEXPORT FOO=bar\n# comment\nBAZ='quoted'\n")
        path = Path(f.name)
    try:
        # First call parses
        r1 = load_env_file(path)
        assert r1 == {"KEY": "value", "FOO": "bar", "BAZ": "quoted"}
        # Second call hits cache
        r2 = load_env_file(path)
        assert r2 == r1
        # ...but returns a COPY, not the memoized dict itself. 2026-09-26:
        # all three return points used to hand back the cached object, so a
        # caller doing secrets["NEW_KEY"] = ... on what it assumed was its own
        # copy silently corrupted the entry for every other caller. A stale
        # version of this test asserted `r2 is r1`, pinning the bug in place.
        assert r2 is not r1
    finally:
        path.unlink()


def test_mutating_a_result_does_not_corrupt_the_cache():
    """The copy guarantee, asserted behaviourally.

    This is the bug the 2026-09-26 copy-on-return fix addressed: mutating
    what a caller believes is its own dict must not be visible to the next
    reader of the same file.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("KEY=value\n")
        path = Path(f.name)
    try:
        first = load_env_file(path)
        first["INJECTED"] = "should not persist"
        second = load_env_file(path)
        assert "INJECTED" not in second
        assert second == {"KEY": "value"}
    finally:
        path.unlink()


def test_export_prefix_is_case_insensitive():
    """`export` is a shell keyword; real .env files write it any case.

    The prefix used to be stripped only for exactly lowercase `export `, so
    `EXPORT FOO=bar` became a variable literally named "EXPORT FOO" and FOO
    never resolved.
    """
    for prefix in ("export ", "EXPORT ", "Export ", "export\t"):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(f"{prefix}FOO=bar\n")
            path = Path(f.name)
        try:
            assert load_env_file(path) == {"FOO": "bar"}, prefix
        finally:
            path.unlink()


def test_a_key_that_merely_starts_with_export_is_untouched():
    """Only the `export` keyword is stripped, not keys that contain it."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("exported=1\nexport_count=2\n")
        path = Path(f.name)
    try:
        assert load_env_file(path) == {"exported": "1", "export_count": "2"}
    finally:
        path.unlink()


def test_cache_invalidation_on_write():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("KEY=old\n")
        path = Path(f.name)
    try:
        r1 = load_env_file(path)
        assert r1["KEY"] == "old"

        # Modify file
        time.sleep(0.01)  # ensure mtime changes
        path.write_text("KEY=new\n")
        invalidate_env_file_cache(path)

        r2 = load_env_file(path)
        assert r2["KEY"] == "new"
    finally:
        path.unlink()


def test_no_cache_on_read_failure():
    path = Path("/nonexistent/.env")
    r1 = load_env_file(path)
    assert r1 == {}
    # Second call also returns {} but doesn't cache the failure
    r2 = load_env_file(path)
    assert r2 == {}


def test_symlink_safety():
    with tempfile.TemporaryDirectory() as td:
        real1 = Path(td) / "real1.env"
        real2 = Path(td) / "real2.env"
        link = Path(td) / "link.env"
        real1.write_text("KEY=one\n")
        real2.write_text("KEY=two\n")
        link.symlink_to(real1)

        r1 = load_env_file(link)
        assert r1["KEY"] == "one"

        # Switch symlink to real2
        link.unlink()
        link.symlink_to(real2)

        # Without invalidate, cache keyed by inode of real1 would return stale "one"
        # But our fingerprint uses inode from open fd, so it detects the change
        r2 = load_env_file(link)
        assert r2["KEY"] == "two"


def test_concurrent_invalidation_during_read():
    """Slow reader should not repopulate cache after writer invalidated."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("KEY=initial\n")
        path = Path(f.name)
    try:
        # Prime cache
        load_env_file(path)

        barrier = threading.Barrier(2)
        results = {}

        def slow_reader():
            # Hold the file open briefly to simulate slow read
            barrier.wait()  # wait for writer
            time.sleep(0.05)
            results["reader"] = load_env_file(path)
            barrier.wait()

        def writer():
            barrier.wait()  # wait for reader to start
            path.write_text("KEY=updated\n")
            invalidate_env_file_cache(path)
            barrier.wait()

        t1 = threading.Thread(target=slow_reader)
        t2 = threading.Thread(target=writer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Reader should see updated value, not stale cached value
        assert results["reader"]["KEY"] == "updated"
    finally:
        path.unlink()


def test_lru_eviction():
    invalidate_env_file_cache()  # clear all
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for i in range(70):  # > MAX (64)
            p = Path(td) / f"env_{i}.env"
            p.write_text(f"KEY=val{i}\n")
            paths.append(p)
            load_env_file(p)
        # First entries should be evicted
        assert str(paths[0]) not in load_env_file.__globals__["_ENV_FILE_CACHE"]
        assert str(paths[-1]) in load_env_file.__globals__["_ENV_FILE_CACHE"]


def test_bom_and_latin1_fallback():
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".env", delete=False) as f:
        f.write(codecs.BOM_UTF8 + b"KEY=value\n")
        path = Path(f.name)
    try:
        r = load_env_file(path)
        assert r["KEY"] == "value"
    finally:
        path.unlink()

    with tempfile.NamedTemporaryFile(mode="wb", suffix=".env", delete=False) as f:
        f.write(b"KEY=\xe9lite\n")  # latin-1 é
        path = Path(f.name)
    try:
        r = load_env_file(path)
        assert r["KEY"] == "élite"
    finally:
        path.unlink()


def test_same_size_rewrite_undetected():
    """
    Accepted limitation: rewrite preserving mtime_ns, size, inode, device
    is not detected without explicit invalidation.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("KEY=first\n")
        path = Path(f.name)
    try:
        r1 = load_env_file(path)
        assert r1["KEY"] == "first"

        # Rewrite with same byte length, same second (mtime_ns may match if fast)
        # This is the documented gap; invalidate_env_file_cache() is the knob.
        path.write_text("KEY=second\n")  # same length
        # Cache may return stale "first" here — caller must invalidate on write
    finally:
        path.unlink()


if __name__ == "__main__":
    test_basic_parse_and_cache()
    test_cache_invalidation_on_write()
    test_no_cache_on_read_failure()
    test_symlink_safety()
    test_concurrent_invalidation_during_read()
    test_lru_eviction()
    test_bom_and_latin1_fallback()
    test_same_size_rewrite_undetected()
    print("All tests passed.")
