from pathlib import Path

from scripts.child_env import destination_child_env as served_profile_child_env


def test_refusal_when_target_missing(tmp_path: Path):
    env, refuse = served_profile_child_env(tmp_path / "gone")
    assert env is None and refuse is not None and "no longer exists" in refuse


def test_refusal_not_raised(tmp_path: Path, monkeypatch):
    # lane does not catch — helper must return refusal strings, not raise
    bad = tmp_path / "boom"
    bad.write_text("I am a file, not a home dir")
    env, refuse = served_profile_child_env(bad)
    assert env is None and refuse


def test_sender_credentials_never_leak(tmp_path: Path):
    base = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "sk-" + "x" * 24,
        "MY_ODD_NAME": "eyJhbGciOiJIUzI1NiJ9.payload.sig",
    }
    env, refuse = served_profile_child_env(tmp_path, base_env=base)
    assert refuse is None
    assert env is not None
    assert "OPENAI_API_KEY" not in env
    assert "MY_ODD_NAME" not in env


def test_target_home_and_tmpdir_set(tmp_path: Path):
    env, refuse = served_profile_child_env(tmp_path, base_env={"PATH": "/usr/bin"})
    assert refuse is None
    assert env is not None
    assert env["SENSEI_HOME"] == str(tmp_path)
    assert env["TMPDIR"] == str(tmp_path / "tmp")


def test_leaked_launch_model_stripped(tmp_path: Path):
    base = {
        "PATH": "/usr/bin",
        "SENSEI_MODEL": "launch-profile-model",
        "SENSEI_TERMINAL_POLICY": "yolo",
    }
    env, refuse = served_profile_child_env(tmp_path, base_env=base)
    assert refuse is None
    assert env is not None
    assert "SENSEI_MODEL" not in env
    assert "SENSEI_TERMINAL_POLICY" not in env


def test_destination_env_file_overlaid(tmp_path: Path):
    (tmp_path / ".env").write_text('DEST_MODEL="destination-model"\n')
    env, refuse = served_profile_child_env(tmp_path, base_env={"PATH": "/usr/bin"})
    assert refuse is None
    assert env is not None
    assert env["DEST_MODEL"] == "destination-model"
    # sender residue does not shadow the destination overlay for masked keys
    assert "SENSEI_MODEL" not in env
