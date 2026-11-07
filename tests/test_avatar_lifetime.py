"""The avatar window's lifetime rules (event-driven, 2026-11-03 operator spec) and the
review findings of 2026-11-04. Pure logic: no display, no GTK window."""

import pytest

from ninja.avatar_approval import reset_tts_cache, should_stay_open, tts_playing


# -- should_stay_open ---------------------------------------------------------
@pytest.mark.parametrize("state", ["listening", "sending", "heard", "working"])
def test_busy_states_always_stay(state):
    assert should_stay_open(state, False, 999.0, False, 999.0) is True


def test_idle_leaves_immediately():
    assert should_stay_open("idle", False, 0.0, False, 0.0) is False


def test_done_stays_while_the_voice_is_talking():
    assert should_stay_open("done", True, 60.0, True, 0.0) is True


def test_done_waits_for_speech_to_start_then_gives_up():
    assert (
        should_stay_open("done", False, 5.0, False, 0.0) is True
    )  # TTS still generating
    assert should_stay_open("done", False, 11.0, False, 0.0) is False  # never started


def test_done_leaves_one_second_after_the_voice_stops():
    assert should_stay_open("done", False, 20.0, True, 0.5) is True
    assert should_stay_open("done", False, 20.0, True, 1.5) is False


def test_a_done_card_never_outlives_three_minutes():
    # even if something keeps looking like speech, a dead producer must not pin the window
    assert should_stay_open("done", True, 181.0, True, 0.0) is False


# -- tts_playing against a fake process table --------------------------------------
def _proc(root, pid, argv, state="S"):
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "stat").write_text(f"{pid} (x) {state} 1 1 1")


def test_script_player_counts(tmp_path):
    _proc(
        tmp_path,
        101,
        ["/home/u/ai-controller/scripts/hermes_tts_play.sh", "/tmp/a.mp3"],
    )
    assert tts_playing(str(tmp_path)) is True


def test_script_run_through_a_shell_counts(tmp_path):
    _proc(tmp_path, 102, ["bash", "/x/hermes_tts_play.sh", "/tmp/a.mp3"])
    assert tts_playing(str(tmp_path)) is True


def test_any_non_shell_player_tagged_for_barge_in_counts(tmp_path):
    for pid, prog in ((103, "mpv"), (104, "ffplay"), (105, "paplay")):
        _proc(tmp_path, pid, [prog, "--force-media-title=AI_TTS_BARGE", "/tmp/a.mp3"])
        # round 4, item 4: without this the 0.3 s memo answers for the second and
        # third players, so only the first was ever really checked.
        reset_tts_cache()
        assert tts_playing(str(tmp_path)) is True
        for f in (tmp_path / str(pid)).iterdir():
            f.unlink()
        (tmp_path / str(pid)).rmdir()


def test_a_shell_that_only_mentions_the_name_is_not_a_player(tmp_path):
    _proc(tmp_path, 106, ["bash", "-c", "pgrep -f hermes_tts_play ; echo AI_TTS_BARGE"])
    assert tts_playing(str(tmp_path)) is False


def test_a_finished_player_that_was_never_reaped_is_not_speaking(tmp_path):
    _proc(tmp_path, 107, ["/x/hermes_tts_play.sh", "/tmp/a.mp3"], state="Z")
    assert tts_playing(str(tmp_path)) is False


def test_nothing_playing(tmp_path):
    _proc(tmp_path, 108, ["firefox"])
    assert tts_playing(str(tmp_path)) is False
    assert tts_playing(str(tmp_path / "missing")) is False


# -- reading the caption file ------------------------------------------------------
from ninja.avatar_approval import interpret_caption  # noqa: E402


def test_a_previous_takes_idle_cannot_close_a_new_window():
    t0 = 1000.0
    assert (
        interpret_caption({"state": "idle", "time": t0 - 2.0}, t0) is None
    )  # race found in review
    assert interpret_caption({"state": "idle", "time": t0 + 0.2}, t0)[0] == "idle"


def test_the_press_caption_written_just_before_the_window_still_counts():
    t0 = 1000.0
    state, _t, text = interpret_caption({"state": "listening", "time": t0 - 0.5}, t0)
    assert state == "listening" and "Listening" in text


def test_an_empty_done_still_shows_a_done_card():
    state, _t, text = interpret_caption(
        {"state": "done", "text": "", "time": 1000.1}, 1000.0
    )
    assert state == "done" and text == "Done."


def test_done_text_is_shown_and_capped():
    _s, _t, text = interpret_caption(
        {"state": "done", "text": "x" * 5000, "time": 1000.1}, 1000.0
    )
    assert text.startswith("Done:\n") and len(text) == len("Done:\n") + 1200


def test_other_states_have_their_own_text():
    assert (
        interpret_caption({"state": "working", "text": "t", "time": 1}, 0)[2]
        == "Working on:\nt"
    )
    assert interpret_caption({"state": "heard", "text": "hi", "time": 1}, 0)[
        2
    ].startswith("Heard")


# -- review round 3 (2026-11-04) ---------------------------------------------------
def test_a_program_that_only_mentions_the_tag_is_not_speech(tmp_path):
    """Finding 1: grep / python -c carrying AI_TTS_BARGE in argv pinned the card
    to its 180s cap. Only a real player counts."""
    _proc(tmp_path, 109, ["python3", "-c", "import os; os.environ['AI_TTS_BARGE']='1'"])
    _proc(tmp_path, 110, ["grep", "-r", "AI_TTS_BARGE", "/tmp"])
    reset_tts_cache()
    assert tts_playing(str(tmp_path)) is False


def test_the_cache_is_short_enough_for_the_one_second_quiet_rule(tmp_path):
    """Round 4, item 3: a 1 s memo stretched "leave 1 s after the voice stops" to ~2 s."""
    from ninja import avatar_approval as av

    # Round 5, item 2: below the 500ms poll the memo never hits at all, so it has
    # to sit just above it -- and still well under the 1s silence rule.
    assert 0.5 < av._TTS_CACHE_TTL_S <= 1.0
    _proc(tmp_path, 111, ["mpv", "--force-media-title=AI_TTS_BARGE", "/tmp/a.mp3"])
    reset_tts_cache()
    assert tts_playing(str(tmp_path)) is True
    # the player dies; a memo longer than the 1 s gap would keep reporting speech
    for f in (tmp_path / "111").iterdir():
        f.unlink()
    (tmp_path / "111").rmdir()
    reset_tts_cache()
    assert tts_playing(str(tmp_path)) is False


def test_a_done_written_just_before_the_window_is_not_the_new_answer():
    """Finding 7: the 1s grace belongs to "listening" only. A previous take's
    "done" 0.5s before the window showed the last answer as the new one."""
    t0 = 1000.0
    assert (
        interpret_caption({"state": "done", "text": "old", "time": t0 - 0.5}, t0)
        is None
    )
    assert (
        interpret_caption({"state": "done", "text": "new", "time": t0}, t0)[0] == "done"
    )
    # the press caption keeps its grace
    assert (
        interpret_caption({"state": "listening", "time": t0 - 0.5}, t0)[0]
        == "listening"
    )


def test_show_avatar_never_reflows_a_run_or_approval_card(monkeypatch):
    """Round 4, item 2 (HIGH). show_avatar carries run AND approval cards; forcing
    prose=True re-flowed a command or path that was being approved. Only the
    lifetime loop's "done" answer card may ask for sentence spacing."""
    import types

    from ninja import state_machine as sm

    seen = {}

    class _Fake:
        _win = types.SimpleNamespace(get_visible=lambda: True)
        machine = types.SimpleNamespace(set_state=lambda *a, **k: None)

        def set_agent(self, agent):
            pass

        def set_card_text(self, text, prose=False):
            seen["prose"], seen["text"] = prose, text

        def _set_active(self, active):
            pass

        def show(self):
            pass

    monkeypatch.setattr(sm, "get_overlay", lambda **kw: _Fake())
    card = "WHO: agent\nWHAT: rm -rf ~/x\nWHERE: /home\nWHEN: now\nHOW: shell"
    sm.show_avatar("tahuti", card)
    assert seen["prose"] is False  # not re-flowed
    assert seen["text"] == card  # byte-for-byte what the gate showed


def test_hermes_own_player_from_the_voice_dir_counts(tmp_path):
    """Round 5, item 3: Hermes\' ffplay/aplay carry no barge tag, so they were
    invisible and long replies were cut off mid-sentence."""
    _proc(tmp_path, 112, ["ffplay", "-nodisp", "-autoexit", "/tmp/hermes_voice/x.mp3"])
    reset_tts_cache()
    assert tts_playing(str(tmp_path)) is True


def test_a_wrapper_with_a_flag_before_the_script_still_counts(tmp_path):
    """Round 5, item 4: argv[1] can be an option, not the script."""
    _proc(tmp_path, 113, ["bash", "-x", "/x/hermes_tts_play.sh", "/tmp/a.mp3"])
    reset_tts_cache()
    assert tts_playing(str(tmp_path)) is True


def test_a_lookalike_binary_name_does_not_count(tmp_path):
    """Round 5, item 4: exact script names only -- no prefix matching."""
    _proc(tmp_path, 114, ["/usr/local/bin/hermes_tts_play_fake", "-q", "AI_TTS_BARGE"])
    reset_tts_cache()
    assert tts_playing(str(tmp_path)) is False
