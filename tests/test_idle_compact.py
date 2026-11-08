"""Isolated unit tests for the idle-compaction snapshot state machine."""

import copy
import sys
import threading
import time

import idle_compact
from idle_compact import (
    IDLE_COMPACTION_LOCAL_ONLY,
    IdleCompactor,
    fingerprint,
    split_history,
)


def history():
    return [{"role": "system", "content": "rules"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
        for i in range(16)
    ]


def prepared(items, session="one"):
    worker = IdleCompactor(lambda _messages, _cancel: "summary", clock=lambda: 500)
    worker._due = 0
    cancel = threading.Event()
    worker._prepare(
        copy.deepcopy(items), session, fingerprint(items), worker._generation, cancel
    )
    return worker


def test_idle_threshold_and_disabled(monkeypatch):
    class FakeTimer:
        def __init__(self, delay, callback, args):
            self.callback, self.args = callback, args
            self.daemon = False

        def start(self):
            pass

        def cancel(self):
            pass

        def join(self):
            pass

    monkeypatch.setattr(idle_compact.threading, "Timer", FakeTimer)
    now = [100.0]
    calls = []
    worker = IdleCompactor(
        lambda *_: calls.append(1) or "summary", clock=lambda: now[0]
    )
    worker.arm(history(), "one", 0, trusted=True)
    assert worker._timer is None
    worker.arm(history(), "one", 300, trusted=True)
    timer = worker._timer
    assert timer is not None
    now[0] = 399
    timer.callback(*timer.args)
    assert not calls
    now[0] = 400
    timer.callback(*timer.args)
    assert calls == [1]
    worker.cancel()


def test_busy_confirmation_and_stream_never_start():
    worker = IdleCompactor(lambda *_: "bad")
    worker.arm(history(), "one", 300, trusted=True, busy=True)
    assert worker._timer is None


def test_prepare_does_not_mutate_live_history():
    items = history()
    before = copy.deepcopy(items)
    prepared(items)
    assert items == before


def test_next_turn_splices_once_preserves_tail_and_tool_groups():
    items = history()
    items[-13] = {"role": "assistant", "tool_calls": [{"id": "call-1"}], "content": ""}
    items[-12] = {"role": "tool", "tool_call_id": "call-1", "content": "result"}
    parts = split_history(items)
    assert parts is not None and parts[2][0] == items[-13]
    worker = prepared(items)
    assert worker.consume(items, "one")
    assert items[-13:] == parts[2]
    assert not worker.consume(items, "one")


def test_revision_or_content_change_invalidates():
    items = history()
    worker = prepared(items)
    items[2]["content"] = "redacted"
    before = copy.deepcopy(items)
    assert not worker.consume(items, "one")
    assert items == before


def test_failure_timeout_and_cancel_preserve_history():
    items = history()
    before = copy.deepcopy(items)
    worker = IdleCompactor(lambda *_: "", clock=lambda: 500)
    worker._prepare(
        copy.deepcopy(items), "one", fingerprint(items), 0, threading.Event()
    )
    assert not worker.consume(items, "one")
    assert items == before


def test_session_switch_and_stale_worker_cannot_publish():
    items = history()
    worker = prepared(items)
    assert not worker.consume(items, "another")
    worker.cancel()
    worker._prepare(
        copy.deepcopy(items), "one", fingerprint(items), 0, threading.Event()
    )
    assert not worker.consume(items, "one")


def test_private_history_never_uses_unapproved_cloud(monkeypatch):
    assert IDLE_COMPACTION_LOCAL_ONLY is True
    worker = IdleCompactor(lambda *_: "bad")
    worker.arm(history(), "one", 300, trusted=False)
    assert worker._timer is None
    calls = []
    monkeypatch.setattr(
        idle_compact, "_cancellable_command", lambda *args: calls.append(args) or ""
    )
    for model in ("", "gpt-oss:cloud", "ollama-cloud::example"):
        assert idle_compact.local_summarizer(model, history(), threading.Event()) == ""
    assert not calls
    assert (
        idle_compact.local_summarizer("local:latest", history(), threading.Event())
        == ""
    )
    assert len(calls) == 1  # unavailable local model never starts inference
    calls.clear()
    monkeypatch.setattr(idle_compact.os.path, "isfile", lambda _: True)

    def fake_local(*args):
        calls.append(args)
        return "FROM /local/model" if len(calls) == 1 else 'IDLE_SUMMARY_RESULT="done"'

    monkeypatch.setattr(idle_compact, "_cancellable_command", fake_local)
    assert (
        idle_compact.local_summarizer("local:latest", history(), threading.Event())
        == "done"
    )
    assert calls[1][0][0] == sys.executable
    assert "master_ai.ask_local" in calls[1][0][2]


def test_cancel_stops_only_the_spawned_child():
    cancel = threading.Event()
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            idle_compact._cancellable_command(
                [sys.executable, "-c", "import time; time.sleep(30)"], "", cancel, 10
            )
        )
    )
    thread.start()
    time.sleep(0.3)
    cancel.set()
    thread.join(5)
    assert not thread.is_alive()
    assert results == [""]
