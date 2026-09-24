from pathlib import Path

import pytest

from scripts import (
    approval_scoped,
    auxiliary_health_scoped,
    credential_files_scoped,
    profile_scope,
    redact_scoped,
)


def test_default_scope_uses_process_home():
    p = profile_scope.current()
    assert p.name == "local"
    assert p.resolved_home == Path.home()


def test_routed_profile_sees_own_cwd(tmp_path):
    local = profile_scope.Profile(name="local", home_dir=tmp_path)
    sandbox = profile_scope.Profile(name="sandbox", home_dir=tmp_path)
    assert local.resolved_cwd == tmp_path
    assert sandbox.resolved_cwd == Path("/tmp/sensei-sandbox").resolve()


def test_scoped_env_overlays_and_includes_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED", "launch")
    profile = profile_scope.Profile(
        name="work", home_dir=tmp_path, env={"SHARED": "scoped", "SECRET": "x"}
    )
    with profile_scope.with_profile(profile):
        env = profile_scope.scoped_env()
        assert env["SHARED"] == "scoped"
        assert env["SECRET"] == "x"
        assert env["SENSEI_PROFILE"] == "work"
        assert env["SENSEI_HOME"] == str(tmp_path)
        assert env["SENSEI_CWD"] == str(profile.resolved_cwd)


def test_allowlist_isolated_per_profile(tmp_path):
    alice = profile_scope.Profile(name="alice", home_dir=tmp_path / "alice")
    bob = profile_scope.Profile(name="bob", home_dir=tmp_path / "bob")

    approval_scoped.allow(["git status"], profile=alice)
    approval_scoped.allow(["npm install"], profile=bob)

    assert approval_scoped.is_allowed("git status", profile=alice)
    assert not approval_scoped.is_allowed("npm install", profile=alice)
    assert approval_scoped.is_allowed("npm install", profile=bob)


def test_redaction_resolves_profile_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSEI_REDACT_SECRETS", "false")
    profile = profile_scope.Profile(
        name="work", home_dir=tmp_path, env={"SENSEI_REDACT_SECRETS": "true"}
    )
    assert redact_scoped.redaction_enabled(profile) is True

    no_env = profile_scope.Profile(name="work", home_dir=tmp_path, config={"security": {"redact_secrets": False}})
    assert redact_scoped.redaction_enabled(no_env) is False


def test_credential_mounts_relative_to_profile_home(tmp_path):
    profile = profile_scope.Profile(name="work", home_dir=tmp_path)
    mounts = credential_files_scoped.resolve_mounts(
        {"aws": ".aws/credentials"}, profile=profile
    )
    assert mounts["aws"] == tmp_path / ".aws" / "credentials"


def test_auxiliary_health_cache_isolated_by_profile():
    alice = profile_scope.Profile(name="alice", home_dir=Path("/home/alice"))
    bob = profile_scope.Profile(name="bob", home_dir=Path("/home/bob"))

    auxiliary_health_scoped.mark_unhealthy("openai", ttl_seconds=300, profile=alice)
    assert auxiliary_health_scoped.is_unhealthy("openai", profile=alice)
    assert not auxiliary_health_scoped.is_unhealthy("openai", profile=bob)


def test_cache_key_contains_profile():
    alice = profile_scope.Profile(name="alice", home_dir=Path("/home/alice"))
    bob = profile_scope.Profile(name="bob", home_dir=Path("/home/bob"))
    assert profile_scope.cache_key("x", profile=alice) != profile_scope.cache_key(
        "x", profile=bob
    )
