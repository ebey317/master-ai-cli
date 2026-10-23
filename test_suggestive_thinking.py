"""Tests for the suggestive-thinking loop skeleton (GATHER->...->DELIVER).

passes_relevance_filter itself is a TODO(human) stub (see suggestive_thinking.py) --
these tests cover the loop plumbing (gather/recall/compare/deliver wiring,
logging, silent-by-default behavior) by monkeypatching the filter, without
depending on its actual threshold logic.
"""

from __future__ import annotations

import json

import suggestive_thinking as st


def _candidate(**overrides):
    base = {"source": "test", "headline": "test headline"}
    base.update(overrides)
    return st.Candidate(**base)


def test_run_loop_calls_deliver_only_when_filter_says_surface(monkeypatch):
    delivered = []
    monkeypatch.setattr(
        st, "passes_relevance_filter", lambda c: (c.headline == "surface-me", "stub")
    )
    candidates = [_candidate(headline="surface-me"), _candidate(headline="stay-quiet")]
    results = st.run_loop(
        gather=lambda: candidates, deliver=lambda c: delivered.append(c.headline)
    )
    assert delivered == ["surface-me"]
    assert [r.surfaced for r in results] == [True, False]


def test_run_loop_is_silent_by_default_on_empty_gather(monkeypatch):
    monkeypatch.setattr(st, "passes_relevance_filter", lambda c: (False, "stub"))
    results = st.run_loop(gather=lambda: [])
    assert results == []


def test_run_loop_logs_every_decision(tmp_path, monkeypatch):
    log_path = tmp_path / "log.jsonl"
    monkeypatch.setattr(st, "LOG", log_path)
    monkeypatch.setattr(st, "passes_relevance_filter", lambda c: (False, "not timely"))
    st.run_loop(gather=lambda: [_candidate(headline="quiet-one")])
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["surfaced"] is False
    assert entry["reason"] == "not timely"
    assert entry["headline"] == "quiet-one"


def test_recall_and_compare_are_wired_through(monkeypatch):
    seen_by_recall = []
    seen_by_compare = []

    def recall(cs):
        cs = list(cs)
        seen_by_recall.extend(cs)
        return cs

    def compare(cs):
        cs = list(cs)
        seen_by_compare.extend(cs)
        return cs

    monkeypatch.setattr(st, "passes_relevance_filter", lambda c: (False, "stub"))
    st.run_loop(gather=lambda: [_candidate()], recall=recall, compare=compare)
    assert len(seen_by_recall) == 1
    assert len(seen_by_compare) == 1


def _scored(**overrides):
    base = {
        "novelty": 0.8,
        "actionable": 0.8,
        "timely": 0.8,
        "interruption_worthy": 0.8,
        "outcome": 0.8,
    }
    base.update(overrides)
    return _candidate(**base)


def test_filter_surfaces_when_all_five_scores_clear_threshold():
    surfaced, reason = st.passes_relevance_filter(_scored())
    assert surfaced is True
    assert reason == "all five scores clear threshold"


def test_filter_stays_silent_when_any_score_is_below_threshold():
    surfaced, reason = st.passes_relevance_filter(_scored(actionable=0.3))
    assert surfaced is False
    assert "actionable=0.30" in reason


def test_filter_reports_the_worst_failing_dimension():
    surfaced, reason = st.passes_relevance_filter(_scored(actionable=0.5, timely=0.2))
    assert surfaced is False
    assert "timely=0.20" in reason  # 0.2 is worse than 0.5, named even though both fail


def test_filter_boundary_at_exact_threshold_passes():
    surfaced, _ = st.passes_relevance_filter(_scored(novelty=st.RELEVANCE_THRESHOLD))
    assert surfaced is True


def test_time_critical_bypasses_all_four_scores():
    candidate = _candidate(
        novelty=0.0,
        actionable=0.0,
        timely=0.0,
        interruption_worthy=0.0,
        time_critical=True,
    )
    surfaced, reason = st.passes_relevance_filter(candidate)
    assert surfaced is True
    assert reason == "time_critical override"
