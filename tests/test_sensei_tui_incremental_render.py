"""Incremental chat rendering (T34): same visible text as a full parse, no re-parse of old chunks."""

import sys

import pytest

pytest.importorskip("prompt_toolkit")

from prompt_toolkit.formatted_text import ANSI  # noqa: E402

import sensei_tui as st  # noqa: E402


def _app(monkeypatch):
    monkeypatch.setattr(sys, "argv", [sys.argv[0]])
    return st.SenseiApp()


def _plain(frags):
    return "".join(f[1] for f in frags)


def test_incremental_matches_full_parse(monkeypatch):
    app = _app(monkeypatch)
    for s in ["hello \x1b[31mred\x1b[0m\n", "plain line\n", "\x1b[1mbold\x1b[0m end\n"]:
        app.write(s)
        app._render_output()
    full = ANSI("".join(app._output_chunks)).__pt_formatted_text__()
    got = list(app._render_output())
    assert _plain(got) == _plain(full)
    assert app._output_render_lines == "".join(app._output_chunks).count("\n")


def test_old_chunks_are_not_reparsed(monkeypatch):
    app = _app(monkeypatch)
    app.write("one\n")
    app._render_output()
    calls = []
    real = st.ANSI

    def spy(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(st, "ANSI", spy)
    app.write("two\n")
    app._render_output()
    assert len(calls) == 1 and "two" in calls[0] and "one" not in calls[0]


def test_clear_and_trim_reset_the_cache(monkeypatch):
    app = _app(monkeypatch)
    app.write("keep\n")
    app._render_output()
    app.clear_output()
    app.write("fresh\n")
    assert "keep" not in _plain(app._render_output())
    monkeypatch.setattr(st, "_OUTPUT_BUFFER_CAP_BYTES", 50)
    monkeypatch.setattr(st, "_OUTPUT_BUFFER_TRIM_TO", 20)
    for i in range(30):
        app.write(f"line {i:03d}\n")
        app._render_output()
    text = _plain(app._render_output())
    assert "line 029" in text and "line 000" not in text


def test_merge_fragments_joins_same_style_runs():
    frags = [("a", "x"), ("a", "y"), ("b", "z"), ("a", "w")]
    assert st._merge_fragments(frags) == [("a", "xy"), ("b", "z"), ("a", "w")]
