"""Tests for suggestive_notify — the native notification/decision half
of Suggestive Thinking (2026-10-25 design law: native, model-agnostic,
not piped). Everything here runs offline: sends and approval execution
are injected fakes, state lives in tmp_path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import suggestive_notify as sn


@dataclass
class FakeCandidate:
    source: str = "test-source"
    headline: str = "a headline"
    question: str = "Want me to run the tests?"
    deliverable: str = "a green/red report"
    detail: str = "repo has not been checked in 6 days"
    action_type: str = ""
    action_payload: dict | None = None


class FakeSend:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[dict] = []

    def __call__(self, text, reply_markup=None):
        self.calls.append({"text": text, "reply_markup": reply_markup})
        if self.ok:
            return {"ok": True, "message_id": 4242, "error": None}
        return {"ok": False, "message_id": None, "error": "boom"}


def _state_path(tmp_path):
    return tmp_path / "state.json"


def test_notify_candidate_sends_with_decision_buttons_and_records(tmp_path):
    send = FakeSend()
    record = sn.notify_candidate(
        FakeCandidate(),
        "ENTRY1",
        send=send,
        state_path=_state_path(tmp_path),
        now=1000.0,
    )
    assert record["status"] == "awaiting"
    assert record["entry_id"] == "ENTRY1"
    assert record["message_id"] == 4242
    assert len(send.calls) == 1
    buttons = send.calls[0]["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"sg:a:{record['id']}"
    assert buttons[1]["callback_data"] == f"sg:p:{record['id']}"
    text = send.calls[0]["text"]
    # the full who/what/where/when/why/how card
    assert "<b>Who:</b> Master AI — your own framework, running on Mary" in text
    assert "<b>What:</b> a green/red report" in text
    assert "<b>Where:</b> test source" in text
    assert "<b>When:</b> As soon as you tap \u2705 Do it" in text
    assert "<b>Why:</b> repo has not been checked in 6 days" in text
    assert "<b>How:</b> Want me to run the tests?" in text
    assert "<b>Model:</b>" in text
    # persisted
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["items"][record["id"]]["status"] == "awaiting"
    assert state["items"][record["id"]]["card"]["what"] == "a green/red report"


def test_notify_candidate_escapes_html(tmp_path):
    send = FakeSend()
    sn.notify_candidate(
        FakeCandidate(question="Want me to fix <b>this</b> & that?"),
        None,
        send=send,
        state_path=_state_path(tmp_path),
        now=1000.0,
    )
    text = send.calls[0]["text"]
    assert "<b>this</b>" not in text
    assert "&lt;b&gt;this&lt;/b&gt; &amp; that?" in text


def test_notify_candidate_send_failure_is_recorded_not_raised(tmp_path):
    record = sn.notify_candidate(
        FakeCandidate(),
        None,
        send=FakeSend(ok=False),
        state_path=_state_path(tmp_path),
        now=1.0,
    )
    assert record["status"] == "send_failed"


def test_parse_callback():
    assert sn.parse_callback("sg:a:20261025120000") == ("approve", "20261025120000")
    assert sn.parse_callback("sg:p:20261025120000") == ("pass", "20261025120000")
    assert sn.parse_callback("sg:x:1") is None
    assert sn.parse_callback("hello") is None
    assert sn.parse_callback("") is None


def _seed_one(tmp_path, entry_id="ENTRY1", now=1000.0):
    send = FakeSend()
    record = sn.notify_candidate(
        FakeCandidate(), entry_id, send=send, state_path=_state_path(tmp_path), now=now
    )
    return record


def test_handle_decision_approve_runs_the_handler_once(tmp_path):
    record = _seed_one(tmp_path)
    calls = []

    def fake_approve(entry_id):
        calls.append(entry_id)
        return True, "ran: GREEN"

    ok, text = sn.handle_decision(
        record["id"],
        "approve",
        state_path=_state_path(tmp_path),
        approve_fn=fake_approve,
        now=2000.0,
    )
    assert ok
    assert calls == ["ENTRY1"]
    assert "GREEN" in text
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["items"][record["id"]]["status"] == "approved"
    # idempotent: a second tap does not re-run the work
    ok, text = sn.handle_decision(
        record["id"],
        "approve",
        state_path=_state_path(tmp_path),
        approve_fn=fake_approve,
        now=3000.0,
    )
    assert ok and "Already decided" in text
    assert calls == ["ENTRY1"]


def test_handle_decision_pass_rejects_and_marks_passed(tmp_path):
    record = _seed_one(tmp_path)
    rejected = []
    ok, text = sn.handle_decision(
        record["id"],
        "pass",
        state_path=_state_path(tmp_path),
        reject_fn=lambda eid: rejected.append(eid) or (True, "rejected"),
        now=2000.0,
    )
    assert ok
    assert rejected == ["ENTRY1"]
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["items"][record["id"]]["status"] == "passed"


def test_handle_decision_without_entry_just_acknowledges(tmp_path):
    record = _seed_one(tmp_path, entry_id=None)
    ok, text = sn.handle_decision(
        record["id"], "approve", state_path=_state_path(tmp_path), now=2000.0
    )
    assert ok and "noted" in text


def test_handle_decision_unknown_id(tmp_path):
    ok, text = sn.handle_decision("nope", "approve", state_path=_state_path(tmp_path))
    assert not ok


def test_digest_batches_unseen_into_one_message(tmp_path):
    path = _state_path(tmp_path)
    send = FakeSend()
    r1 = sn.notify_candidate(
        FakeCandidate(question="Q one?"), "E1", send=send, state_path=path, now=0.0
    )
    r2 = sn.notify_candidate(
        FakeCandidate(question="Q two?"), "E2", send=send, state_path=path, now=0.0
    )
    digest_send = FakeSend()
    later = 13 * 3600.0  # past the 12h unseen TTL
    sent = sn.maybe_send_digest(send=digest_send, state_path=path, now=later)
    assert sent is not None
    assert sorted(sent["digest_of"]) == sorted([r1["id"], r2["id"]])
    assert len(digest_send.calls) == 1  # ONE batched message, not two nags
    text = digest_send.calls[0]["text"]
    assert "Q one?" in text and "Q two?" in text
    rows = digest_send.calls[0]["reply_markup"]["inline_keyboard"]
    assert len(rows) == 2  # one decision row per item
    # cooldown: an immediate second digest does not fire
    assert (
        sn.maybe_send_digest(send=digest_send, state_path=path, now=later + 60) is None
    )
    assert len(digest_send.calls) == 1


def test_describe_executor_names_deterministic_and_free_first():
    assert sn.describe_executor("repo_test_check").startswith(
        "no AI model — plain code"
    )
    assert sn.describe_executor("").startswith("no AI model — no worker")
    model_line = sn.describe_executor("some_future_model_handler")
    assert "free/cheap first" in model_line


def test_resolve_work_model_prefers_free_then_active(tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps(
            {
                "models": [
                    {"id": "paid/x", "pricing": {"prompt": "1", "completion": "1"}},
                    {
                        "id": "free/y:free",
                        "pricing": {"prompt": "0", "completion": "0"},
                    },
                ]
            }
        )
    )
    active = tmp_path / "active"
    active.write_text("active/model\n")
    monkeypatch.setattr(sn, "FREE_PICKER_CACHE", cache)
    monkeypatch.setattr(sn, "ACTIVE_MODEL_FILE", active)
    assert sn.resolve_work_model() == "free/y:free"
    assert sn.resolve_work_model(huge=True) == "active/model"


def test_digest_skips_decided_and_fresh_items(tmp_path):
    path = _state_path(tmp_path)
    send = FakeSend()
    decided = sn.notify_candidate(
        FakeCandidate(question="Decided?"), "E1", send=send, state_path=path, now=0.0
    )
    sn.handle_decision(
        decided["id"],
        "pass",
        state_path=path,
        reject_fn=lambda eid: (True, ""),
        now=10.0,
    )
    sn.notify_candidate(
        FakeCandidate(question="Fresh?"),
        "E2",
        send=send,
        state_path=path,
        now=13 * 3600.0,
    )
    digest_send = FakeSend()
    sent = sn.maybe_send_digest(send=digest_send, state_path=path, now=13 * 3600.0 + 60)
    assert sent is None  # decided item stays dead, fresh item is not yet unseen
    assert digest_send.calls == []
