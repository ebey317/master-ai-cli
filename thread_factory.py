#!/usr/bin/env python3
"""Curate raw AI chat sessions into published, pruned, auditable project repos.

Three failure modes drive this file's design. Each one produced a real,
observed loss on this machine, and each one silently returns a plausible
result rather than an error:

1. Sampling a conversation loses the conversation. A prior pass read
   `messages[:20] + messages[-40:]` and, on a 327-message session, missed
   100% of the real content -- the decisions all lived in the middle. So
   `build_candidate` concatenates every message in order, and
   `content_hash_of` hashes every message in order. Prompt windowing for a
   bounded model context is a separate, explicitly-marked step
   (`_window_for_prompt`); extraction and hashing are never windowed.

2. Publishing into a repo this tool did not create destroys someone else's
   work. `gh repo list ebey317` has 60 repos; `biovega` looks like Thread
   Factory output and is not -- no `.thread-source.json`, none of the
   taxonomy. The cap-replace path therefore never selects a repo by age
   alone: it re-reads `.thread-source.json` live from the remote
   immediately before touching it, and aborts to the next candidate on a
   404, a diverged HEAD, or any answer it cannot positively confirm.

3. Deleting local content after an unverified push loses the content. This
   tool exists because uncontrolled local disk growth crashed this machine
   (two orphaned `soffice.bin` processes filled 71GB on 2026-09-28), so it
   must prune -- but `git push` exiting 0 is not proof the content is
   fetchable. `prune_local_thread` deletes only after two independent
   confirmations (remote HEAD sha match via `git ls-remote`, plus a live
   `gh api contents/THREAD_MANIFEST.md`), and `audit_phase` re-proves both
   on every later run so a silent regression surfaces instead of hiding.

Every `git`/`gh` invocation uses `start_new_session=True` plus
`os.killpg` on timeout. `gh` forks `git`, and `git` can block forever on a
credential-helper prompt -- the same launcher-orphans-worker shape as the
71GB incident, with a different binary.

No function here returns an empty string to mean failure. Failures return a
diagnostic, so a caller can tell "nothing there" from "could not look".
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

LOG = logging.getLogger("thread_factory")

# ── Paths / defaults ──────────────────────────────────────────────────────
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "thread-factory" / "config.json"
DEFAULT_CATALOG_PATH = (
    Path.home() / ".local" / "state" / "thread-factory" / "catalog.json"
)
CATALOG_VERSION = 1

# The eight folders a LIVE thread gets. One folder per DistilledThread field
# that carries content, plus `_extracted` for the raw session backup. Fixed
# and ordered so two runs of the tool -- and a human reading two different
# published repos -- see the same shape every time.
THREAD_FOLDERS: tuple[str, ...] = (
    "_extracted",
    "authority",
    "memory",
    "ideas",
    "artifacts",
    "references",
    "decisions",
    "tasks",
)
AUTHORITY_FILES: tuple[str, ...] = ("AGENTS.md", "PRINCIPLES.md", "CONVENTIONS.md")
MARKER_FILENAME = ".thread-source.json"
MANIFEST_FILENAME = "THREAD_MANIFEST.md"

# ── Subprocess budgets ────────────────────────────────────────────────────
GH_TIMEOUT = 45.0
GIT_NET_TIMEOUT = 180.0
GIT_LOCAL_TIMEOUT = 60.0

# Retries are asymmetric on purpose. A read can be repeated safely; a write
# cannot -- retrying `gh repo create` after an ambiguous failure is how you
# end up with two repos and burn a slot against the cap.
READ_RETRIES = 1
READ_RETRY_BACKOFF_S = 2.0

# ── Prompt windowing ──────────────────────────────────────────────────────
TRIAGE_CONTEXT_CHARS = 12_000
DISTILL_CONTEXT_CHARS = 90_000
MIN_FIELD_CHARS = 40

# Two tiers, not one flat list. A single regex over both classes produced a
# real false-positive: a genuine 300-word memory entry that says "the team
# decided to stop using TODO markers in this repo" would be rejected outright
# because "\bTODO\b" matches anywhere in the text, with no regard for how
# much of the text it actually is. The fix distinguishes what CAN legitimately
# appear inside otherwise-real prose from what CANNOT:
#
# - STRUCTURAL markers (a bare heading with nothing under it, a line that is
#   ONLY "n/a"/"none", a template token like "<DESCRIPTION>" or "{{slug}}")
#   are never something a real, substantive paragraph would also contain --
#   matching them anywhere is safe regardless of surrounding length.
# - KEYWORD markers ("TODO", "coming soon", "lorem ipsum", "placeholder"
#   itself) ARE plausible topics of real engineering discussion, so they only
#   count as placeholder evidence when the field is ALSO short -- a genuine
#   stub like "TODO: fill this in" is short BY CONSTRUCTION; a 300-word
#   paragraph that happens to mention the word "TODO" once is not.
_STRUCTURAL_PLACEHOLDER_PATTERNS: tuple[str, ...] = (
    r"^\s*#{1,6}\s*(memory|ideas|artifacts|references|decisions|tasks|notes|readme|overview)\s*$",
    r"^\s*(?:n/?a|none|nothing|-{1,3})\s*$",
    r"<[A-Z][A-Z0-9_]{2,}>",
    r"\{\{[^}]{1,80}\}\}",
    r"\[insert[^\]]{0,80}\]",
)
_KEYWORD_PLACEHOLDER_PATTERNS: tuple[str, ...] = (
    r"\bTODO\b",
    r"\bTBD\b",
    r"\bFIXME\b",
    r"\bXXX{1,}\b",
    r"\bplaceholder\b",
    r"\bcoming soon\b",
    r"\blorem ipsum\b",
    r"\bto be (?:filled|written|added|determined|completed)\b",
    r"\bno (?:content|information) (?:available|yet)\b",
)
# A genuine keyword-only placeholder is short by construction ("TODO",
# "TBD: revisit"); this is generous enough that no real distilled field would
# accidentally fall under it while still catching every realistic stub.
_KEYWORD_PLACEHOLDER_MAX_CHARS = 120

_STRUCTURAL_PLACEHOLDER_RE = re.compile(
    "|".join(_STRUCTURAL_PLACEHOLDER_PATTERNS), re.IGNORECASE | re.MULTILINE
)
_KEYWORD_PLACEHOLDER_RE = re.compile(
    "|".join(_KEYWORD_PLACEHOLDER_PATTERNS), re.IGNORECASE
)
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9\s-]")
_SLUG_SPACE_RE = re.compile(r"\s+")
_SLUG_DASH_RE = re.compile(r"-{2,}")

Verdict = Literal["LIVE", "ARCHIVE", "MERGE", "DELETE"]


class ThreadFactoryError(Exception):
    """Base for every error this module raises on purpose."""


class ConfigError(ThreadFactoryError):
    """Configuration is missing or unusable. Always names the offending key."""


class PlaceholderContentError(ThreadFactoryError):
    """A distilled field was empty, too short, or template boilerplate.

    Raised before any file is written. Publishing boilerplate is worse than
    publishing nothing: it looks like real output to everyone downstream.
    """


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fail(subject: object, reason: str) -> str:
    return f"[thread-factory: {subject}: {reason}]"


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def diagnostic(self) -> str:
        detail = (self.stderr or self.stdout or "").strip().replace("\n", " ")
        if not detail:
            detail = "no output on stdout or stderr"
        return f"`{' '.join(self.argv)}` exit {self.returncode}: {detail[:400]}"


RunFn = Callable[..., CommandResult]


def run_command(
    argv: list[str],
    *,
    timeout: float = GIT_LOCAL_TIMEOUT,
    cwd: Path | None = None,
    stdin_text: str | None = None,
) -> CommandResult:
    """Run one command, never raise, never leave an orphan behind.

    `gh` forks `git`, and `git` opens a credential helper that can wait on a
    terminal that is not there. `subprocess.run(timeout=)` kills only the
    process it started -- exactly the shape that filled 71GB of disk on this
    machine with `soffice.bin`. `start_new_session=True` puts the whole tree
    in one process group so the timeout handler can kill all of it.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/true"
    env["GCM_INTERACTIVE"] = "never"
    env.setdefault("GH_PROMPT_DISABLED", "1")
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd) if cwd else None,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        return CommandResult(tuple(argv), -1, "", f"{argv[0]} is not installed")
    except OSError as e:
        return CommandResult(tuple(argv), -1, "", f"could not spawn {argv[0]}: {e}")

    try:
        stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _killpg(proc)
        proc.wait()
        return CommandResult(tuple(argv), -1, "", f"timed out after {timeout:.0f}s")
    except BaseException as e:
        _killpg(proc)
        proc.wait()
        return CommandResult(tuple(argv), -1, "", f"{type(e).__name__}: {e}")

    return CommandResult(tuple(argv), proc.returncode, stdout or "", stderr or "")


def _killpg(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_read_command(
    argv: list[str], *, timeout: float = GH_TIMEOUT, run: RunFn = run_command
) -> CommandResult:
    """A read-only command, retried on transport failure only. A clean
    non-zero exit (a real 404/auth error) is an answer and returns immediately."""
    result = run(argv, timeout=timeout)
    attempts = 0
    while attempts < READ_RETRIES and result.returncode == -1:
        attempts += 1
        LOG.warning("read retry %d/%d: %s", attempts, READ_RETRIES, result.diagnostic())
        time.sleep(READ_RETRY_BACKOFF_S * attempts)
        result = run(argv, timeout=timeout)
    return result


@dataclass(frozen=True)
class ThreadFactoryConfig:
    sessions_dir: Path
    threads_dir: Path
    catalog_path: Path
    github_owner: str  # no default — load_config() errors loudly if missing
    repo_visibility: Literal["private", "public"]
    live_repo_cap: int
    triage_model: str
    distill_model: str
    min_session_messages: int
    harvest_min_similarity: float
    dry_run: bool


@dataclass
class Session:
    session_id: str
    path: Path
    title: str
    created_at: float
    updated_at: float
    messages: list[dict[str, Any]]


@dataclass
class ThreadCandidate:
    session_id: str
    slug: str
    title: str
    message_count: int
    full_text: str  # every message, concatenated, never sampled
    content_hash: str  # sha256 over full_text — the incrementality key


@dataclass
class TriageVerdict:
    session_id: str
    verdict: Verdict
    rationale: str
    merge_target_slug: str | None
    confidence: float


@dataclass
class DistilledThread:
    slug: str
    title: str
    manifest_frontmatter: dict[str, Any]
    readme_body: str
    authority: dict[
        str, str
    ]  # {"AGENTS.md": "...", "PRINCIPLES.md": "...", "CONVENTIONS.md": "..."}
    memory_body: str
    ideas: list[str]
    artifacts: dict[str, str]
    references: list[str]
    decisions: list[str]
    tasks: list[str]


@dataclass
class GithubRepo:
    name: str
    url: str
    pushed_at: str
    is_thread_factory_managed: bool


@dataclass
class CatalogEntry:
    slug: str
    session_id: str
    content_hash: str
    verdict: Verdict
    github_repo_url: str | None
    last_pushed_commit_sha: str | None
    last_pushed_at: str | None
    last_processed_at: str
    pruned_locally: bool


@dataclass
class PublishResult:
    slug: str
    repo_url: str | None
    action: Literal["created", "replaced_inactive", "updated", "skipped"]
    replaced_repo: str | None
    pushed_ok: bool
    pushed_commit_sha: str | None
    detail: str


@dataclass
class AuditResult:
    slug: str
    checks: dict[str, bool]
    passed: bool
    detail: list[str]


# This module runs standalone on any machine with Python 3.10+, git, gh, and
# a reachable Ollama server -- nothing below REQUIRES the sibling modules
# harvest.py or master_ai.py that also happen to live in this repo. Both are
# imported lazily, inside try/except, purely as OPTIONAL acceleration: if
# they're importable (because this is running inside the author's own
# master-ai-cli checkout), triage/distill calls get response caching and
# access to that repo's model router; if they're not (a stranger's install,
# or this file copied out on its own), every call falls straight through to
# a direct HTTP POST against local Ollama, with identical behavior. A
# stranger installing just this one file loses nothing but a cache.

LLMCallFn = Callable[[str, str, str], str]
LLM_ERROR_PREFIX = "[LLM ERROR]"
DEFAULT_LLM_MODEL = "qwen2.5:7b"
LLM_NUM_PREDICT = 2400


def default_llm_call(
    task_type: str,
    system: str,
    user: str,
    *,
    model: str = DEFAULT_LLM_MODEL,
    min_similarity: float = 0.80,
) -> str:
    """Cache-first model call. Never raises; returns an LLM_ERROR_PREFIX string.

    `harvest.lookup()`/`harvest.record()` and `master_ai.ask_model_router()`
    are OPTIONAL: both are imported lazily inside try/except and their
    absence (ModuleNotFoundError, ImportError) is caught right alongside
    every other reason they might fail, so a stranger without either module
    gets exactly the direct-Ollama path below with no special-casing needed.
    Checked first only when present, so re-triaging an unchanged session
    never re-pays for a model call when the cache IS available."""
    cache_key = f"{system}\n\n---\n\n{user}"
    try:
        import harvest

        # harvest.py predates this module's strict typing and has no
        # annotations of its own; under --strict that makes any call into it
        # a real no-untyped-call error, not a false positive. The ignore is
        # scoped to this one optional integration point, not the module.
        cached, similarity, _entry = harvest.lookup(  # type: ignore[no-untyped-call]
            cache_key, min_similarity=min_similarity, task_type=task_type
        )
        if cached:
            LOG.debug("llm cache hit (%s, sim=%.2f)", task_type, similarity)
            return str(cached)
    except Exception as e:
        LOG.debug("harvest lookup unavailable: %s", e)

    reply = _model_chat(model, system, user)
    if reply.startswith(LLM_ERROR_PREFIX):
        return reply
    if not reply.strip():
        return (
            f"{LLM_ERROR_PREFIX} model {model} returned an empty reply for {task_type}"
        )

    try:
        import harvest

        harvest.record(cache_key, model, reply, task_type=task_type)  # type: ignore[no-untyped-call]
    except Exception as e:
        LOG.debug("harvest record failed (non-fatal): %s", e)
    return reply


def _model_chat(model: str, system: str, user: str, *, timeout: float = 240.0) -> str:
    """Model-agnostic chat, adapted from sensei_reasoning_loop._model_chat.
    Returns reply text or an LLM_ERROR_PREFIX diagnostic."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        import master_ai

        if hasattr(master_ai, "ask_model_router"):
            # Same rationale as the harvest calls above: master_ai.py is a
            # large, pre-existing, untyped module -- this optional call into
            # it is a real no-untyped-call under --strict, scoped here.
            text, _elapsed = master_ai.ask_model_router(  # type: ignore[no-untyped-call]
                messages, model=model, max_tokens=LLM_NUM_PREDICT
            )
            if text and text.strip():
                return str(text)
    except Exception as e:
        LOG.debug("master_ai router unavailable, falling back to Ollama: %s", e)

    import urllib.error
    import urllib.request

    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": LLM_NUM_PREDICT, "temperature": 0.2},
            "keep_alive": "30m",
        }
    ).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return f"{LLM_ERROR_PREFIX} ollama HTTP {e.code} for model {model}"
    except urllib.error.URLError as e:
        return f"{LLM_ERROR_PREFIX} ollama unreachable ({e.reason}) for model {model}"
    except (OSError, ValueError) as e:
        return f"{LLM_ERROR_PREFIX} {type(e).__name__} calling ollama: {e}"
    content = ((data.get("message") or {}).get("content") or "").strip()
    if not content:
        return f"{LLM_ERROR_PREFIX} ollama returned no content for model {model}"
    return content


def _parse_json_lenient(text: str) -> tuple[dict[str, Any] | None, str]:
    """Vendored from sensei_reasoning_loop so parsing can't drift between callers."""
    if not text:
        return None, ""
    try:
        parsed = json.loads(text)
        return (parsed if isinstance(parsed, dict) else None), text
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict):
                return parsed, text
        except ValueError:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed, text
        except ValueError:
            pass
    return None, text


def make_llm_call(config: ThreadFactoryConfig, *, phase: str) -> LLMCallFn:
    """Bind a config's model choice into the three-arg LLMCallFn shape.
    Without this, `config.triage_model`/`config.distill_model` would be
    silently ignored -- the exact hardcoded-default leak the
    installable-by-a-stranger test guards against."""
    model = config.triage_model if phase == "triage" else config.distill_model
    similarity = config.harvest_min_similarity

    def _call(task_type: str, system: str, user: str) -> str:
        return default_llm_call(
            task_type, system, user, model=model, min_similarity=similarity
        )

    return _call


CONFIG_TEMPLATE: dict[str, Any] = {
    "sessions_dir": "~/.hermes/webui/sessions",
    "threads_dir": "~/threads",
    "github_owner": None,
    "repo_visibility": "private",
    "live_repo_cap": 20,
    "triage_model": "qwen2.5:7b",
    "distill_model": "qwen2.5:7b",
    "min_session_messages": 4,
    "harvest_min_similarity": 0.80,
    "dry_run": False,
}


def load_config(path: Path | None = None) -> ThreadFactoryConfig:
    """Read the JSON config, or fail loudly naming the exact bad key.
    `github_owner` has no default: guessing it would publish a stranger's
    private conversations into whatever account this machine is
    authenticated as -- an unrecoverable mistake made silently."""
    config_path = (path or DEFAULT_CONFIG_PATH).expanduser()
    if not config_path.exists():
        raise ConfigError(
            f"no config at {config_path}. Create it with:\n"
            f"  mkdir -p {config_path.parent} && "
            f"cat > {config_path} <<'EOF'\n{json.dumps(CONFIG_TEMPLATE, indent=2)}\nEOF\n"
            f'then set "github_owner" to your GitHub username.'
        )
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ConfigError(f"{config_path} is not valid JSON: {e}") from e
    except OSError as e:
        raise ConfigError(f"cannot read {config_path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{config_path} must contain a JSON object, got {type(raw).__name__}"
        )

    unknown = sorted(set(raw) - set(CONFIG_TEMPLATE) - {"catalog_path"})
    if unknown:
        LOG.warning("%s: ignoring unknown key(s): %s", config_path, ", ".join(unknown))

    def _path_of(key: str) -> Path:
        value = raw.get(key, CONFIG_TEMPLATE.get(key))
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f'{config_path}: "{key}" must be a non-empty path string')
        return Path(value).expanduser()

    def _int_of(key: str, minimum: int) -> int:
        value = raw.get(key, CONFIG_TEMPLATE[key])
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ConfigError(
                f'{config_path}: "{key}" must be an integer >= {minimum}, got {value!r}'
            )
        return value

    owner = raw.get("github_owner")
    if not isinstance(owner, str) or not owner.strip():
        raise ConfigError(
            f'{config_path}: "github_owner" is required and is currently {owner!r}. '
            "Set it to the GitHub account that should own published threads. "
            "There is no default on purpose -- publishing to a guessed owner is "
            "not something you can undo."
        )

    visibility = raw.get("repo_visibility", CONFIG_TEMPLATE["repo_visibility"])
    if visibility not in ("private", "public"):
        raise ConfigError(
            f'{config_path}: "repo_visibility" must be "private" or "public", got {visibility!r}'
        )

    similarity = raw.get(
        "harvest_min_similarity", CONFIG_TEMPLATE["harvest_min_similarity"]
    )
    if not isinstance(similarity, (int, float)) or not 0.0 <= float(similarity) <= 1.0:
        raise ConfigError(
            f'{config_path}: "harvest_min_similarity" must be a number in [0.0, 1.0], got {similarity!r}'
        )

    dry_run = raw.get("dry_run", CONFIG_TEMPLATE["dry_run"])
    if not isinstance(dry_run, bool):
        raise ConfigError(
            f'{config_path}: "dry_run" must be true or false, got {dry_run!r}'
        )

    catalog_raw = raw.get("catalog_path")
    catalog_path = (
        Path(catalog_raw).expanduser()
        if isinstance(catalog_raw, str)
        else DEFAULT_CATALOG_PATH
    )

    sessions_dir = _path_of("sessions_dir")
    if not sessions_dir.is_dir():
        raise ConfigError(
            f'{config_path}: "sessions_dir" {sessions_dir} is not a readable directory'
        )

    return ThreadFactoryConfig(
        sessions_dir=sessions_dir,
        threads_dir=_path_of("threads_dir"),
        catalog_path=catalog_path,
        github_owner=owner.strip(),
        repo_visibility=visibility,
        live_repo_cap=_int_of("live_repo_cap", 1),
        triage_model=str(raw.get("triage_model", CONFIG_TEMPLATE["triage_model"])),
        distill_model=str(raw.get("distill_model", CONFIG_TEMPLATE["distill_model"])),
        min_session_messages=_int_of("min_session_messages", 1),
        harvest_min_similarity=float(similarity),
        dry_run=dry_run,
    )


class Catalog:
    """JSON-backed per-slug state with atomic writes under an flock.
    JSON, not SQLite: a few hundred rows, one writer per run, and a human
    debugging a 3am cron failure should be able to `jq` it directly."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.lock_path = self.path.with_suffix(".lock")
        self._by_slug: dict[str, CatalogEntry] = {}
        self._loaded = False

    def load(self) -> None:
        """A corrupt file is preserved and reported, never silently
        overwritten -- it is the only record of what was already published."""
        self._by_slug = {}
        self._loaded = True
        if not self.path.exists():
            LOG.info("no catalog at %s; starting empty (first run)", self.path)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            quarantine = self.path.with_name(
                f"{self.path.name}.corrupt-{int(time.time())}"
            )
            try:
                shutil.copy2(self.path, quarantine)
            except OSError:
                quarantine = Path("(copy failed)")
            LOG.error(
                "catalog %s is unreadable (%s); preserved a copy at %s and starting "
                "empty. Already-published threads will be re-triaged, not lost.",
                self.path,
                e,
                quarantine,
            )
            return

        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            LOG.error('catalog %s has no "entries" list; starting empty', self.path)
            return

        valid_fields = set(CatalogEntry.__dataclass_fields__)
        for row in entries:
            if not isinstance(row, dict):
                LOG.warning("catalog: skipping non-object row %r", row)
                continue
            try:
                entry = CatalogEntry(
                    **{k: v for k, v in row.items() if k in valid_fields}
                )
            except TypeError as e:
                LOG.warning(
                    "catalog: skipping malformed row %r (%s)", row.get("slug"), e
                )
                continue
            self._by_slug[entry.slug] = entry
        LOG.info("catalog: loaded %d entries from %s", len(self._by_slug), self.path)

    def save(self) -> None:
        """Atomic write under an exclusive lock: tmp-file + fsync + os.replace
        + directory fsync. A crash leaves either the previous complete
        catalog or the new complete one -- never a truncated file."""
        if not self._loaded:
            raise ThreadFactoryError("Catalog.save() before Catalog.load()")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CATALOG_VERSION,
            "updated_at": _utc_now(),
            "entries": [
                asdict(e) for e in sorted(self._by_slug.values(), key=lambda e: e.slug)
            ],
        }
        blob = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(self.path.parent), prefix=".catalog-", suffix=".tmp"
                )
                tmp = Path(tmp_name)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(blob)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, self.path)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
                dir_fd = os.open(str(self.path.parent), os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def get(self, slug: str) -> CatalogEntry | None:
        return self._by_slug.get(slug)

    def upsert(self, entry: CatalogEntry) -> None:
        self._by_slug[entry.slug] = entry

    def by_session(self, session_id: str) -> CatalogEntry | None:
        return next(
            (e for e in self._by_slug.values() if e.session_id == session_id), None
        )

    def is_unchanged(self, session_id: str, content_hash: str) -> bool:
        entry = self.by_session(session_id)
        return entry is not None and entry.content_hash == content_hash

    def all_entries(self) -> list[CatalogEntry]:
        return sorted(self._by_slug.values(), key=lambda e: e.slug)

    def live_slugs(self) -> list[str]:
        """Slugs eligible as MERGE targets: LIVE and actually published."""
        return [
            e.slug
            for e in self.all_entries()
            if e.verdict == "LIVE" and e.github_repo_url
        ]

    def thread_factory_repo_names(self) -> set[str]:
        """A hint for narrowing API calls -- never the authority. The
        authority is always a live `.thread-source.json` read."""
        names: set[str] = set()
        for entry in self.all_entries():
            if entry.github_repo_url:
                names.add(
                    entry.github_repo_url.rstrip("/")
                    .rsplit("/", 1)[-1]
                    .removesuffix(".git")
                )
        return names


def discover_sessions(sessions_dir: Path) -> list[Path]:
    """Every *.json directly under sessions_dir, oldest mtime first -- so a
    run that dies partway through has processed the backlog, not today's
    sessions repeatedly while the old ones never get reached."""
    try:
        found = [
            p for p in sessions_dir.iterdir() if p.is_file() and p.suffix == ".json"
        ]
    except OSError as e:
        LOG.error("cannot list sessions_dir %s: %s", sessions_dir, e)
        return []

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(found, key=_mtime)


def _message_text(message: dict[str, Any]) -> str:
    """Hermes WebUI writes `content` as a string; some exports write `text`,
    and multimodal turns write a list of `{"type": "text", "text": ...}`
    parts. Reading only `content` drops whole turns."""
    for key in ("content", "text", "message"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            parts = [
                str(p.get("text", ""))
                for p in value
                if isinstance(p, dict) and p.get("type") in (None, "text")
            ]
            joined = "\n".join(p for p in parts if p.strip())
            if joined.strip():
                return joined
    return ""


def load_session(path: Path) -> Session | None:
    """Parse one session file. Never raises; None + logged diagnostic on failure."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        LOG.warning("%s", _fail(path, f"not valid JSON: {e}"))
        return None
    except OSError as e:
        LOG.warning("%s", _fail(path, f"cannot read: {e}"))
        return None
    if not isinstance(raw, dict):
        LOG.warning(
            "%s", _fail(path, f"top level is {type(raw).__name__}, expected object")
        )
        return None

    messages = raw.get("messages")
    if not isinstance(messages, list):
        LOG.warning("%s", _fail(path, 'has no "messages" list'))
        return None
    clean = [m for m in messages if isinstance(m, dict)]
    if len(clean) != len(messages):
        LOG.warning(
            "%s: dropped %d non-object message(s)", path, len(messages) - len(clean)
        )

    session_id = str(raw.get("session_id") or path.stem).strip()
    if not session_id:
        LOG.warning("%s", _fail(path, "empty session_id and empty filename stem"))
        return None

    def _ts(key: str) -> float:
        value = raw.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    title = str(raw.get("title") or "").strip() or f"Session {session_id[:12]}"
    return Session(
        session_id=session_id,
        path=path,
        title=title,
        created_at=_ts("created_at"),
        updated_at=_ts("updated_at"),
        messages=clean,
    )


def content_hash_of(session: Session) -> str:
    """sha256 over every message, in order. Hashes role+text only -- a
    re-saved-but-unedited session must not look like new content."""
    digest = hashlib.sha256()
    digest.update(session.session_id.encode("utf-8"))
    for message in session.messages:
        digest.update(b"\x1e")
        digest.update(str(message.get("role", "")).encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(_message_text(message).encode("utf-8"))
    return digest.hexdigest()


def backup_session(session: Session, backup_dir: Path) -> Path:
    """Unconditional, first, never deleted -- the only recovery path after a prune."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{session.session_id}.json"
    try:
        shutil.copy2(session.path, target)
    except OSError as e:
        raise ThreadFactoryError(
            f"could not back up {session.path} to {target}: {e}. Refusing to "
            "continue: the backup is the only recovery path after a prune."
        ) from e
    return target


def slugify(text: str) -> str:
    slug = text.lower().replace("&", " and ").replace("_", "-").replace("/", "-")
    slug = _SLUG_STRIP_RE.sub("", slug)
    slug = _SLUG_SPACE_RE.sub("-", slug.strip())
    slug = _SLUG_DASH_RE.sub("-", slug).strip("-")
    return slug[:60].strip("-") or "untitled-thread"


def _unique_slug(base: str, session_id: str, catalog: Catalog) -> str:
    """The SAME session re-slugging to the same value is expected; only a
    collision with a DIFFERENT session_id gets a numeric suffix."""
    existing = catalog.get(base)
    if existing is None or existing.session_id == session_id:
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        owner = catalog.get(candidate)
        if owner is None or owner.session_id == session_id:
            return candidate
    raise ThreadFactoryError(
        f"could not find a free slug for {base!r} after 998 attempts"
    )


def build_candidate(session: Session, catalog: Catalog) -> ThreadCandidate:
    """The whole conversation, concatenated in order. Never sampled -- the
    only bound applied anywhere is `_window_for_prompt`, at the model call."""
    blocks: list[str] = []
    for index, message in enumerate(session.messages, 1):
        role = str(message.get("role", "unknown")).strip() or "unknown"
        text = _message_text(message)
        if not text.strip():
            continue
        blocks.append(f"### [{index}] {role}\n{text.rstrip()}")
    return ThreadCandidate(
        session_id=session.session_id,
        slug=_unique_slug(slugify(session.title), session.session_id, catalog),
        title=session.title,
        message_count=len(session.messages),
        full_text="\n\n".join(blocks),
        content_hash=content_hash_of(session),
    )


def extract_phase(
    config: ThreadFactoryConfig, catalog: Catalog
) -> list[ThreadCandidate]:
    """Back up everything (cheap, unconditional); return only what changed
    (the incrementality gate)."""
    backup_dir = config.threads_dir / "_extracted" / "_sessions-backup"
    candidates: list[ThreadCandidate] = []
    seen = skipped_short = skipped_unchanged = 0

    for path in discover_sessions(config.sessions_dir):
        session = load_session(path)
        if session is None:
            continue
        seen += 1
        backup_session(session, backup_dir)

        if len(session.messages) < config.min_session_messages:
            skipped_short += 1
            continue

        digest = content_hash_of(session)
        if catalog.is_unchanged(session.session_id, digest):
            skipped_unchanged += 1
            continue
        candidates.append(build_candidate(session, catalog))

    LOG.info(
        "extract: %d sessions read, %d backed up, %d too short, %d unchanged, %d to process",
        seen,
        seen,
        skipped_short,
        skipped_unchanged,
        len(candidates),
    )
    return candidates


TRIAGE_SYSTEM = """You are the triage stage of Thread Factory, a pipeline that turns raw \
AI chat sessions into published project repositories.

You are given one conversation and the slugs of threads that already have \
their own repository. Decide what happens to this conversation. A repository \
is a real, permanent artifact with a maintenance cost, so the bar for \
creating one is high.

Choose exactly one verdict:

LIVE     - This conversation is a coherent, ongoing piece of work that \
justifies its own repository: it has real decisions, real artifacts (code, \
configs, designs), and work that continues past the end of the conversation. \
A future reader would open this repo to answer a question.
ARCHIVE  - Real content worth keeping, but it does not justify its own \
repository: a one-off question answered, a debugging session that ended, \
research with no follow-up. This is the default when you are unsure.
MERGE    - This conversation is a continuation of an existing thread listed \
in EXISTING LIVE THREADS. Set merge_target_slug to that exact slug, copied \
character for character from the list. Only use MERGE when the match is \
obvious; a loosely related topic is ARCHIVE, not MERGE.
DELETE   - Nothing of value: an empty exchange, a test message, a duplicate \
with no content, or a conversation that never got past a failed setup step. \
Use this rarely and only when you are certain.

Rules:
- When in doubt between LIVE and ARCHIVE, choose ARCHIVE. An archived thread \
costs one small markdown file; a wrongly-promoted thread permanently consumes \
one of a limited number of repository slots.
- Never choose MERGE unless merge_target_slug is one of the exact strings in \
EXISTING LIVE THREADS.
- Never choose DELETE for a conversation that contains any code, any file \
path, any decision, or any instruction. Deletion is not recoverable from here.
- confidence is your own honest probability, between 0.0 and 1.0, that this \
verdict is the right one.

Respond with ONLY a JSON object, no prose before or after:
{"verdict": "LIVE"|"ARCHIVE"|"MERGE"|"DELETE", "rationale": "one or two \
sentences naming the specific evidence in the conversation that decided it", \
"merge_target_slug": "exact-slug-or-null", "confidence": 0.0}"""

TRIAGE_USER_TEMPLATE = """EXISTING LIVE THREADS (valid merge_target_slug values, exact strings):
{existing_slugs}

THREAD TITLE: {title}
MESSAGE COUNT: {message_count}

CONVERSATION:
{conversation}

Produce the triage JSON now."""

DISTILL_SYSTEM = """You are the distillation stage of Thread Factory. You are \
given one complete AI chat session that has been judged worth publishing as \
its own repository. Turn it into that repository's content.

You are writing for a specific reader: an engineer who was not present for \
this conversation, opening this repository in six months to answer a \
question. Everything you write must be specific to THIS conversation. \
Generic advice, restated headings, and template text are worse than useless \
here -- they look like content and carry none, so they are rejected.

Produce a JSON object with exactly these keys:

  title                 - a human title for the thread, under 80 characters.
  manifest_frontmatter  - a JSON object of metadata: "summary" (2-3 sentences \
on what this work is), "status" (one of "active", "paused", "complete"), \
"topics" (array of 3-8 lowercase topic strings), "started" (date or period \
if the conversation names one, else the empty string).
  readme_body           - the README.md body in markdown, 200-800 words: what \
this is, why it exists, what state it is in, and what a reader should look at \
first. Do not include a top-level heading; one is added for you.
  authority             - an object with exactly three keys, "AGENTS.md", \
"PRINCIPLES.md", "CONVENTIONS.md". AGENTS.md: instructions an AI agent needs \
to work on this project correctly, drawn from what the conversation actually \
established. PRINCIPLES.md: the durable rules and values this work committed \
to. CONVENTIONS.md: the concrete naming, structure, and process conventions \
used. Each must be real markdown of at least a few sentences, specific to \
this conversation. If the conversation genuinely established nothing for one \
of these, write what a reader should assume instead and say why -- never \
leave it a heading.
  memory_body           - markdown: the durable facts a future reader or agent \
must know. Decisions already made, dead ends already hit, things that are \
true about the environment. This is the single most valuable file; write it \
carefully.
  ideas                 - array of strings: concrete ideas raised and not yet \
acted on. Empty array if there genuinely were none.
  artifacts             - an object mapping filename to full file content for \
every substantial code block, config, or script produced in the conversation. \
Use the real filename if the conversation named one, otherwise a descriptive \
one with the right extension. Empty object if there were none.
  references            - array of strings: URLs, file paths, tools, and \
documents the conversation relied on, each with a few words on what it is.
  decisions             - array of strings: each a decision that was actually \
made, phrased as "decided X because Y".
  tasks                 - array of strings: work identified and not completed, \
each phrased as a concrete action.

Rules:
- Never write "TODO", "TBD", "placeholder", "coming soon", "N/A", or a bare \
heading as the value of any field. A field with nothing real to say gets a \
real sentence explaining that and why, not a token.
- Never invent facts the conversation does not support.
- Arrays and objects may be empty when the conversation genuinely had none of \
that thing; strings may not.

Respond with ONLY the JSON object, no prose before or after."""

DISTILL_USER_TEMPLATE = """THREAD SLUG: {slug}
THREAD TITLE: {title}
MESSAGE COUNT: {message_count}
TRIAGE RATIONALE: {rationale}

FULL CONVERSATION:
{conversation}

Produce the distillation JSON now."""


def _window_for_prompt(text: str, limit: int) -> str:
    """The ONLY place content is reduced -- never reaches full_text, the
    hash, or the backup. The marker matters: a silently-truncated
    conversation gets confidently summarized as if it were the whole thing."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    elided = len(text) - limit
    return (
        text[:head]
        + f"\n\n[... {elided} characters of this conversation elided to fit the "
        f"model context; {head} characters from the start and {tail} from the "
        f"end are shown ...]\n\n" + text[-tail:]
    )


def triage_session(
    candidate: ThreadCandidate,
    existing_live_slugs: list[str],
    llm_call: LLMCallFn = default_llm_call,
) -> TriageVerdict:
    """Every failure path lands on ARCHIVE -- not LIVE (never burn a repo
    slot on a judgment that never happened), not DELETE (never destroy data
    on an LLM hiccup)."""
    user = TRIAGE_USER_TEMPLATE.format(
        existing_slugs="\n".join(f"- {s}" for s in existing_live_slugs) or "(none yet)",
        title=candidate.title,
        message_count=candidate.message_count,
        conversation=_window_for_prompt(candidate.full_text, TRIAGE_CONTEXT_CHARS),
    )
    reply = llm_call("thread_factory_triage", TRIAGE_SYSTEM, user)

    def _archive(reason: str) -> TriageVerdict:
        LOG.warning("triage fell back to ARCHIVE for %s: %s", candidate.slug, reason)
        return TriageVerdict(
            session_id=candidate.session_id,
            verdict="ARCHIVE",
            rationale=f"fallback to ARCHIVE: {reason}",
            merge_target_slug=None,
            confidence=0.0,
        )

    if reply.startswith(LLM_ERROR_PREFIX):
        return _archive(reply)
    parsed, raw = _parse_json_lenient(reply)
    if parsed is None:
        return _archive(f"reply was not JSON: {raw[:200]!r}")

    verdict_raw = str(parsed.get("verdict", "")).strip().upper()
    if verdict_raw not in ("LIVE", "ARCHIVE", "MERGE", "DELETE"):
        return _archive(f"unknown verdict {verdict_raw!r}")

    rationale = str(parsed.get("rationale", "")).strip() or "(model gave no rationale)"
    target = parsed.get("merge_target_slug")
    target = str(target).strip() if isinstance(target, str) and target.strip() else None
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    if verdict_raw == "MERGE" and target not in existing_live_slugs:
        return _archive(f"MERGE named unknown target {target!r}")

    return TriageVerdict(
        session_id=candidate.session_id,
        verdict=verdict_raw,  # type: ignore[arg-type]
        rationale=rationale,
        merge_target_slug=target if verdict_raw == "MERGE" else None,
        confidence=confidence,
    )


def is_placeholder(text: str, *, min_chars: int = MIN_FIELD_CHARS) -> bool:
    """Three independent tests, because each misses what the others catch:

    1. A length floor (the old script's "# Memory\n" is 9 characters).
    2. A structural pattern -- a bare heading, a whole line that is only
       "n/a", a template token -- matched ANYWHERE regardless of length,
       since real prose never legitimately contains these shapes.
    3. A keyword pattern ("TODO", "coming soon", ...) matched ONLY when the
       text is ALSO short. Matching these anywhere regardless of length was
       a real bug: a genuine, substantive memory entry that says "the team
       decided to stop using TODO markers in this repo" would be rejected
       outright, because the word appears once in 300 real words. A keyword
       is placeholder evidence only in combination with brevity -- a real
       stub like "TODO: fill this in" is short by construction; a real
       paragraph that happens to mention the word is not.
    """
    stripped = (text or "").strip()
    if len(stripped) < min_chars:
        return True
    if _STRUCTURAL_PLACEHOLDER_RE.search(stripped):
        return True
    if len(
        stripped
    ) <= _KEYWORD_PLACEHOLDER_MAX_CHARS and _KEYWORD_PLACEHOLDER_RE.search(stripped):
        return True
    return False


def _require_real(field_name: str, value: str, slug: str) -> str:
    if is_placeholder(value):
        raise PlaceholderContentError(
            f"{slug}: field {field_name!r} is placeholder or too short "
            f"(needs >= {MIN_FIELD_CHARS} real characters): {value.strip()[:120]!r}"
        )
    return value.strip()


def distill_thread(
    candidate: ThreadCandidate,
    verdict: TriageVerdict,
    llm_call: LLMCallFn = default_llm_call,
) -> DistilledThread:
    """Raises rather than returning degraded content. A half-distilled
    thread that publishes looks identical to a good one from the outside, so
    the only safe failure is a loud one that leaves the session untouched
    for the next run to retry."""
    user = DISTILL_USER_TEMPLATE.format(
        slug=candidate.slug,
        title=candidate.title,
        message_count=candidate.message_count,
        rationale=verdict.rationale,
        conversation=_window_for_prompt(candidate.full_text, DISTILL_CONTEXT_CHARS),
    )
    reply = llm_call("thread_factory_distill", DISTILL_SYSTEM, user)
    if reply.startswith(LLM_ERROR_PREFIX):
        raise ThreadFactoryError(
            f"{candidate.slug}: distillation model call failed: {reply}"
        )
    parsed, raw = _parse_json_lenient(reply)
    if parsed is None:
        raise ThreadFactoryError(
            f"{candidate.slug}: distillation reply was not JSON: {raw[:300]!r}"
        )

    frontmatter = parsed.get("manifest_frontmatter")
    if not isinstance(frontmatter, dict):
        raise ThreadFactoryError(
            f"{candidate.slug}: manifest_frontmatter must be an object"
        )
    frontmatter = dict(frontmatter)
    frontmatter.setdefault("slug", candidate.slug)
    frontmatter.setdefault("session_id", candidate.session_id)
    frontmatter.setdefault("message_count", candidate.message_count)
    frontmatter.setdefault("content_hash", candidate.content_hash)
    frontmatter.setdefault("distilled_at", _utc_now())
    _require_real(
        "manifest_frontmatter.summary",
        str(frontmatter.get("summary", "")),
        candidate.slug,
    )

    authority_raw = parsed.get("authority")
    if not isinstance(authority_raw, dict):
        raise ThreadFactoryError(
            f"{candidate.slug}: authority must be an object with keys {AUTHORITY_FILES}"
        )
    authority: dict[str, str] = {}
    for name in AUTHORITY_FILES:
        authority[name] = _require_real(
            f"authority[{name}]", str(authority_raw.get(name, "")), candidate.slug
        )

    def _string_list(key: str) -> list[str]:
        value = parsed.get(key, [])
        if not isinstance(value, list):
            raise ThreadFactoryError(
                f"{candidate.slug}: {key} must be a list, got {type(value).__name__}"
            )
        items = [str(v).strip() for v in value if str(v).strip()]
        for item in items:
            if is_placeholder(item, min_chars=8):
                raise PlaceholderContentError(
                    f"{candidate.slug}: {key} contains placeholder item {item!r}"
                )
        return items

    artifacts_raw = parsed.get("artifacts", {})
    if not isinstance(artifacts_raw, dict):
        raise ThreadFactoryError(
            f"{candidate.slug}: artifacts must be an object of filename -> content"
        )
    artifacts: dict[str, str] = {}
    for name, body in artifacts_raw.items():
        safe_name = Path(str(name)).name  # never let the model write outside artifacts/
        if not safe_name or safe_name.startswith("."):
            LOG.warning(
                "%s: dropping artifact with unusable name %r", candidate.slug, name
            )
            continue
        text = str(body)
        if not text.strip():
            LOG.warning("%s: dropping empty artifact %r", candidate.slug, safe_name)
            continue
        artifacts[safe_name] = text

    return DistilledThread(
        slug=candidate.slug,
        title=str(parsed.get("title", "")).strip() or candidate.title,
        manifest_frontmatter=frontmatter,
        readme_body=_require_real(
            "readme_body", str(parsed.get("readme_body", "")), candidate.slug
        ),
        authority=authority,
        memory_body=_require_real(
            "memory_body", str(parsed.get("memory_body", "")), candidate.slug
        ),
        ideas=_string_list("ideas"),
        artifacts=artifacts,
        references=_string_list("references"),
        decisions=_string_list("decisions"),
        tasks=_string_list("tasks"),
    )


def _bullets(items: list[str], empty_note: str) -> str:
    """A markdown list, or an honest sentence. Never an empty section --
    empty reads as "not filled in"; a sentence saying there were none reads
    as what it is, and survives the audit's placeholder scan."""
    return "\n".join(f"- {item}" for item in items) if items else empty_note


def _marker_payload(
    distilled: DistilledThread,
    session: Session,
    content_hash: str,
    pushed_at: str | None,
) -> dict[str, Any]:
    """The `.thread-source.json` body -- the load-bearing "this repo is
    mine" signal. Its presence on the remote is the ONLY thing that
    authorizes the cap-replace path to overwrite a repo."""
    return {
        "machine": os.uname().nodename,
        "session_id": session.session_id,
        "thread_slug": distilled.slug,
        "content_hash": content_hash,
        "artifacts_count": len(distilled.artifacts),
        "decisions_count": len(distilled.decisions),
        "pushed_at": pushed_at,
        "tool": "thread_factory.py",
    }


def write_thread_folder(
    thread_dir: Path, distilled: DistilledThread, session: Session
) -> None:
    """Write the full eight-folder taxonomy. LIVE only. Validates every
    field before creating a single file, so a placeholder failure leaves no
    half-written directory for the next run to mistake for finished work."""
    for name, body in (
        ("readme_body", distilled.readme_body),
        ("memory_body", distilled.memory_body),
        *((f"authority[{k}]", v) for k, v in distilled.authority.items()),
    ):
        _require_real(name, body, distilled.slug)

    thread_dir.mkdir(parents=True, exist_ok=True)
    for folder in THREAD_FOLDERS:
        (thread_dir / folder).mkdir(exist_ok=True)

    frontmatter_lines = [
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in sorted(distilled.manifest_frontmatter.items())
    ]
    manifest = (
        "---\n" + "\n".join(frontmatter_lines) + "\n---\n\n"
        f"# {distilled.title}\n\n"
        f"## Decisions\n\n{_bullets(distilled.decisions, 'No decisions were recorded in this thread.')}\n\n"
        f"## Open tasks\n\n{_bullets(distilled.tasks, 'No open tasks remain from this thread.')}\n\n"
        f"## References\n\n{_bullets(distilled.references, 'This thread cited no external references.')}\n"
    )
    (thread_dir / MANIFEST_FILENAME).write_text(manifest, encoding="utf-8")
    (thread_dir / "README.md").write_text(
        f"# {distilled.title}\n\n{distilled.readme_body.strip()}\n", encoding="utf-8"
    )

    for name, body in distilled.authority.items():
        (thread_dir / "authority" / name).write_text(
            body.strip() + "\n", encoding="utf-8"
        )
    (thread_dir / "memory" / "MEMORY.md").write_text(
        f"# Memory — {distilled.title}\n\n{distilled.memory_body.strip()}\n",
        encoding="utf-8",
    )
    (thread_dir / "ideas" / "IDEAS.md").write_text(
        f"# Ideas — {distilled.title}\n\n{_bullets(distilled.ideas, 'This thread raised no unacted-on ideas.')}\n",
        encoding="utf-8",
    )
    (thread_dir / "references" / "REFERENCES.md").write_text(
        f"# References — {distilled.title}\n\n"
        f"{_bullets(distilled.references, 'This thread cited no external references.')}\n",
        encoding="utf-8",
    )
    (thread_dir / "decisions" / "DECISIONS.md").write_text(
        f"# Decisions — {distilled.title}\n\n"
        f"{_bullets(distilled.decisions, 'No decisions were recorded in this thread.')}\n",
        encoding="utf-8",
    )
    (thread_dir / "tasks" / "TASKS.md").write_text(
        f"# Tasks — {distilled.title}\n\n{_bullets(distilled.tasks, 'No open tasks remain from this thread.')}\n",
        encoding="utf-8",
    )
    for name, body in distilled.artifacts.items():
        (thread_dir / "artifacts" / name).write_text(body, encoding="utf-8")

    backup_dir = thread_dir / "_extracted" / "_sessions-backup"
    backup_session(session, backup_dir)

    content_hash = str(distilled.manifest_frontmatter.get("content_hash", ""))
    (thread_dir / MARKER_FILENAME).write_text(
        json.dumps(_marker_payload(distilled, session, content_hash, None), indent=2)
        + "\n",
        encoding="utf-8",
    )


def write_archive_summary(archive_dir: Path, summary: str, slug: str) -> Path:
    """One markdown file under threads_dir/_archive/. Never published. Cheap
    on purpose -- what makes ARCHIVE a safe default for every ambiguous triage."""
    _require_real("archive summary", summary, slug)
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{slug}.md"
    target.write_text(
        f"# {slug}\n\n_Archived {_utc_now()} by thread_factory.py — not published._\n\n{summary.strip()}\n",
        encoding="utf-8",
    )
    return target


def append_to_existing_thread(
    target_slug: str,
    summary: str,
    config: ThreadFactoryConfig,
    catalog: Catalog,
    *,
    run: RunFn = run_command,
) -> None:
    """MERGE: append to a thread that already has a repo, republish it.
    Never touches cap math -- repo count doesn't change, so no branch here
    can reach `gh repo create`. A structural guarantee, not a check that
    could be forgotten."""
    _require_real("merge summary", summary, target_slug)
    entry = catalog.get(target_slug)
    if entry is None:
        raise ThreadFactoryError(f"MERGE target {target_slug!r} is not in the catalog")
    if not entry.github_repo_url:
        raise ThreadFactoryError(f"MERGE target {target_slug!r} has no published repo")

    thread_dir = config.threads_dir / target_slug
    if entry.pruned_locally or not thread_dir.exists():
        thread_dir = restore_thread(target_slug, catalog, config, run=run)

    memory_path = thread_dir / "memory" / "MEMORY.md"
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        memory_path.read_text(encoding="utf-8")
        if memory_path.exists()
        else f"# Memory — {target_slug}\n"
    )
    memory_path.write_text(
        f"{existing.rstrip()}\n\n## Merged session {_utc_now()}\n\n{summary.strip()}\n",
        encoding="utf-8",
    )

    result = publish_thread(thread_dir, target_slug, config, catalog, run=run)
    if not result.pushed_ok and not config.dry_run:
        raise ThreadFactoryError(
            f"MERGE into {target_slug} failed to publish: {result.detail}"
        )


def structure_phase(
    candidates: list[ThreadCandidate],
    config: ThreadFactoryConfig,
    catalog: Catalog,
    llm_call: LLMCallFn = default_llm_call,
) -> tuple[list[tuple[ThreadCandidate, TriageVerdict, Path]], list[str]]:
    """Triage and structure every candidate. One failure never stops the
    batch. A candidate that fails distillation gets NO catalog row, so the
    next run retries it -- dropping it silently is the one outcome this
    shape exists to prevent."""
    ready: list[tuple[ThreadCandidate, TriageVerdict, Path]] = []
    diagnostics: list[str] = []
    live_slugs = catalog.live_slugs()

    for candidate in candidates:
        try:
            verdict = triage_session(candidate, live_slugs, llm_call)
            LOG.info(
                "triage %s -> %s (confidence %.2f): %s",
                candidate.slug,
                verdict.verdict,
                verdict.confidence,
                verdict.rationale,
            )

            if verdict.verdict == "DELETE":
                catalog.upsert(_entry_for(candidate, verdict))
                catalog.save()
                continue

            if verdict.verdict == "ARCHIVE":
                distilled = distill_thread(candidate, verdict, llm_call)
                write_archive_summary(
                    config.threads_dir / "_archive",
                    f"{distilled.readme_body}\n\n## Memory\n\n{distilled.memory_body}",
                    candidate.slug,
                )
                catalog.upsert(_entry_for(candidate, verdict))
                catalog.save()
                continue

            if verdict.verdict == "MERGE":
                assert (
                    verdict.merge_target_slug is not None
                )  # guaranteed by triage_session
                distilled = distill_thread(candidate, verdict, llm_call)
                append_to_existing_thread(
                    verdict.merge_target_slug,
                    f"{distilled.readme_body}\n\n{distilled.memory_body}",
                    config,
                    catalog,
                )
                catalog.upsert(_entry_for(candidate, verdict))
                catalog.save()
                continue

            session = load_session(_session_path_for(candidate, config))
            if session is None:
                raise ThreadFactoryError(
                    f"session file for {candidate.session_id} vanished mid-run"
                )
            distilled = distill_thread(candidate, verdict, llm_call)
            thread_dir = config.threads_dir / candidate.slug
            write_thread_folder(thread_dir, distilled, session)
            ready.append((candidate, verdict, thread_dir))

        except (PlaceholderContentError, ThreadFactoryError) as e:
            message = f"{candidate.slug}: {type(e).__name__}: {e}"
            LOG.error("structure failed, will retry next run — %s", message)
            diagnostics.append(message)
        except Exception as e:
            message = f"{candidate.slug}: unexpected {type(e).__name__}: {e}"
            LOG.exception("structure crashed on %s", candidate.slug)
            diagnostics.append(message)

    return ready, diagnostics


def _entry_for(
    candidate: ThreadCandidate, verdict: TriageVerdict, **overrides: Any
) -> CatalogEntry:
    """One constructor so a new field can't be set at one call site and
    forgotten in four others."""
    base: dict[str, Any] = {
        "slug": candidate.slug,
        "session_id": candidate.session_id,
        "content_hash": candidate.content_hash,
        "verdict": verdict.verdict,
        "github_repo_url": None,
        "last_pushed_commit_sha": None,
        "last_pushed_at": None,
        "last_processed_at": _utc_now(),
        "pruned_locally": False,
    }
    base.update(overrides)
    return CatalogEntry(**base)


def _session_path_for(candidate: ThreadCandidate, config: ThreadFactoryConfig) -> Path:
    """Prefer the live session file, fall back to the backup extract_phase always takes."""
    live = config.sessions_dir / f"{candidate.session_id}.json"
    return (
        live
        if live.exists()
        else config.threads_dir
        / "_extracted"
        / "_sessions-backup"
        / f"{candidate.session_id}.json"
    )


def check_publish_preconditions(
    config: ThreadFactoryConfig, *, run: RunFn = run_command
) -> tuple[bool, str]:
    """Verify `gh`/`git` are usable BEFORE anything is created. A missing
    credential discovered halfway through the batch leaves some threads
    published and some not, and re-costs every model call on retry."""
    if shutil.which("git") is None:
        return False, "git is not installed or not on PATH"
    if shutil.which("gh") is None:
        return False, "gh (GitHub CLI) is not installed or not on PATH"
    result = run_read_command(["gh", "auth", "status"], timeout=GH_TIMEOUT, run=run)
    if not result.ok:
        return (
            False,
            f"gh is not authenticated — run `gh auth login`. {result.diagnostic()}",
        )
    return True, f"gh authenticated; publishing to {config.github_owner}"


def is_thread_factory_repo(owner: str, name: str, *, run: RunFn = run_command) -> bool:
    """True only on a provable, live 200 for `.thread-source.json`. This is
    the single check standing between the cap-replace path and destroying a
    repo this tool didn't create -- `biovega` is real and would otherwise be
    a valid-looking replacement target by age alone. Deliberately
    asymmetric: a 404, a rate limit, a timeout, an auth error all return
    False. "Can't confirm it's mine" and "not mine" lead to the same safe
    action -- leave it alone."""
    result = run_read_command(
        [
            "gh",
            "api",
            f"repos/{owner}/{name}/contents/{MARKER_FILENAME}",
            "--jq",
            ".name",
        ],
        timeout=GH_TIMEOUT,
        run=run,
    )
    if result.ok and result.stdout.strip() == MARKER_FILENAME:
        return True
    combined = (result.stderr + result.stdout).lower()
    if "404" in combined or "not found" in combined:
        LOG.debug("%s/%s has no %s (404)", owner, name, MARKER_FILENAME)
    else:
        LOG.warning(
            "could not confirm %s/%s ownership; treating as NOT managed. %s",
            owner,
            name,
            result.diagnostic(),
        )
    return False


def list_owner_repos(owner: str, *, run: RunFn = run_command) -> list[GithubRepo]:
    """Every repo, newest-pushed first, with `is_thread_factory_managed`
    determined by a LIVE marker check on each one -- not a hint from the
    local catalog.

    Trusting the local catalog for this was a real cross-machine bug. This
    tool is meant to run on more than one machine against the same GitHub
    account -- this environment genuinely has two ("Mary" and "Tabby") -- and
    each machine's catalog only knows the repos IT created. A repo Tabby
    created is invisible to Mary's catalog, so Mary's cap count would
    silently undercount the true managed-repo total and could create past
    the real cap while believing it was safely under it. The whole point of
    this design is a correct cap; a count that's wrong across machines makes
    the guarantee worthless. Checking every repo live costs a handful of
    extra API calls against GitHub's 5000/hr authenticated limit, on a tool
    that runs at most every couple of months, or occasionally by hand --
    correctness is worth more than saving that."""
    result = run_read_command(
        ["gh", "repo", "list", owner, "--limit", "500", "--json", "name,url,pushedAt"],
        timeout=GH_TIMEOUT,
        run=run,
    )
    if not result.ok:
        LOG.error("cannot list repos for %s: %s", owner, result.diagnostic())
        return []
    try:
        rows = json.loads(result.stdout or "[]")
    except ValueError as e:
        LOG.error("gh repo list returned unparseable JSON: %s", e)
        return []
    repos = [
        GithubRepo(
            name=str(row.get("name", "")),
            url=str(row.get("url", "")),
            pushed_at=str(row.get("pushedAt", "")),
            is_thread_factory_managed=is_thread_factory_repo(
                owner, str(row.get("name", "")), run=run
            ),
        )
        for row in rows
        if isinstance(row, dict) and row.get("name")
    ]
    return sorted(repos, key=lambda r: r.pushed_at, reverse=True)


def count_live_managed_repos(
    owner: str, catalog: Catalog, *, run: RunFn = run_command
) -> int:
    """Counted against the live account's authoritative marker state, not
    the local catalog -- see `list_owner_repos` for why the catalog alone
    isn't trustworthy once more than one machine can write to the same
    account. `catalog` is accepted for call-site stability (callers already
    have one on hand) but is no longer the source of truth here."""
    del (
        catalog
    )  # kept for signature stability; the live marker check is authoritative now
    return sum(
        1 for r in list_owner_repos(owner, run=run) if r.is_thread_factory_managed
    )


def pick_replacement_target(managed_repos: list[GithubRepo]) -> GithubRepo | None:
    """The least recently pushed managed repo, or None. The
    `is_thread_factory_managed` filter is INSIDE the min(), not applied by
    the caller, so no caller can accidentally skip it -- an unmanaged repo
    can never be returned no matter how old it is."""
    return min(
        (r for r in managed_repos if r.is_thread_factory_managed),
        key=lambda r: (r.pushed_at, r.name),
        default=None,
    )


def verify_pushed_and_retrievable(
    owner: str, repo: str, expected_sha: str, *, run: RunFn = run_command
) -> tuple[bool, str]:
    """Two independent proofs. `git push` exiting 0 is not one of them -- a
    push can succeed onto a branch nothing serves, into a repo that went
    private mid-run, or ahead of API propagation, and all three look
    identical to success at the call site."""
    url = f"https://github.com/{owner}/{repo}.git"
    remote = run_read_command(
        ["git", "ls-remote", url, "HEAD"], timeout=GIT_NET_TIMEOUT, run=run
    )
    if not remote.ok:
        return False, f"git ls-remote failed: {remote.diagnostic()}"
    first_line = (remote.stdout or "").strip().splitlines()
    if not first_line:
        return False, f"git ls-remote returned no refs for {url} (empty repository?)"
    actual_sha = first_line[0].split()[0].strip()
    if actual_sha != expected_sha:
        return False, f"remote HEAD is {actual_sha[:12]}, expected {expected_sha[:12]}"

    api = run_read_command(
        [
            "gh",
            "api",
            f"repos/{owner}/{repo}/contents/{MANIFEST_FILENAME}",
            "--jq",
            ".size",
        ],
        timeout=GH_TIMEOUT,
        run=run,
    )
    if not api.ok:
        return (
            False,
            f"{MANIFEST_FILENAME} is not fetchable from the API: {api.diagnostic()}",
        )
    try:
        size = int((api.stdout or "0").strip())
    except ValueError:
        return False, f"unexpected size from contents API: {api.stdout.strip()[:120]!r}"
    if size <= 0:
        return False, f"{MANIFEST_FILENAME} exists but is {size} bytes"
    return (
        True,
        f"verified: HEAD {actual_sha[:12]} and {MANIFEST_FILENAME} ({size} bytes) both live",
    )


def _git_identity(cwd: Path, *, run: RunFn = run_command) -> list[str]:
    """`-c user.*` flags only when the machine has no identity configured --
    never override a configured one (that would rewrite the operator's own
    authorship)."""
    email = run(
        ["git", "-C", str(cwd), "config", "--get", "user.email"],
        timeout=GIT_LOCAL_TIMEOUT,
    )
    if email.ok and email.stdout.strip():
        return []
    return [
        "-c",
        "user.name=thread-factory",
        "-c",
        "user.email=thread-factory@localhost",
    ]


def _head_sha(repo_dir: Path, *, run: RunFn = run_command) -> tuple[str | None, str]:
    result = run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"], timeout=GIT_LOCAL_TIMEOUT
    )
    if not result.ok:
        return None, result.diagnostic()
    sha = result.stdout.strip()
    return (
        (sha, "") if re.fullmatch(r"[0-9a-f]{40}", sha) else (None, f"bad sha {sha!r}")
    )


def _refresh_marker(
    work_dir: Path, slug: str, session_id: str, content_hash: str
) -> None:
    """Rewrite `.thread-source.json` in place to name the CURRENT thread. On
    a replacement the repo keeps its old name forever (renaming breaks
    bookmarked clone URLs) -- the marker, not the repo name, says which
    thread lives here now; the catalog maps slug -> repo."""
    marker_path = work_dir / MARKER_FILENAME
    payload: dict[str, Any] = {}
    if marker_path.exists():
        try:
            loaded = json.loads(marker_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except ValueError:
            pass
    payload.update(
        {
            "machine": os.uname().nodename,
            "session_id": session_id,
            "thread_slug": slug,
            "content_hash": content_hash,
            "pushed_at": _utc_now(),
            "tool": "thread_factory.py",
        }
    )
    marker_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _commit_all(
    work_dir: Path, message: str, *, run: RunFn = run_command
) -> tuple[str | None, str]:
    add = run(["git", "-C", str(work_dir), "add", "-A"], timeout=GIT_LOCAL_TIMEOUT)
    if not add.ok:
        return None, add.diagnostic()
    identity = _git_identity(work_dir, run=run)
    commit = run(
        [
            "git",
            "-C",
            str(work_dir),
            *identity,
            "commit",
            "-m",
            message,
            "--allow-empty",
        ],
        timeout=GIT_LOCAL_TIMEOUT,
    )
    if not commit.ok:
        return None, commit.diagnostic()
    return _head_sha(work_dir, run=run)


def _copy_tree_into(source: Path, dest: Path) -> None:
    """Make dest content-identical to source, keeping `.git`. Deleting
    rather than merging matters: an overlay would leave the previous
    thread's artifacts mixed into the new one with no sign of it."""
    for child in dest.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in source.iterdir():
        if child.name == ".git":
            continue
        target = dest / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def _push_into_existing_repo(
    thread_dir: Path,
    slug: str,
    repo_name: str,
    config: ThreadFactoryConfig,
    catalog: Catalog,
    *,
    expected_sha: str | None,
    commit_message_prefix: str,
    run: RunFn = run_command,
) -> tuple[bool, str, str | None, str | None]:
    """Clone, replace contents, commit, push. Returns (ok, detail, sha,
    old_slug). Refuses on a diverged HEAD; push is never --force, so a stale
    sha that somehow slips past the explicit check still fails loudly as a
    non-fast-forward rejection rather than silently overwriting."""
    owner = config.github_owner
    url = f"https://github.com/{owner}/{repo_name}.git"
    workspace = Path(tempfile.mkdtemp(prefix=f"thread_factory_{slug}_"))
    try:
        clone_dir = workspace / "repo"
        clone = run(
            ["git", "clone", "--depth", "1", url, str(clone_dir)],
            timeout=GIT_NET_TIMEOUT,
        )
        if not clone.ok:
            return False, f"clone of {url} failed: {clone.diagnostic()}", None, None

        current_sha, sha_err = _head_sha(clone_dir, run=run)
        if current_sha is None:
            return False, f"cannot read {repo_name} HEAD: {sha_err}", None, None
        if expected_sha and current_sha != expected_sha:
            return (
                False,
                f"ABORT: {repo_name} HEAD is {current_sha[:12]} but the catalog "
                f"recorded {expected_sha[:12]} — something changed this repo since "
                "thread-factory last wrote it; refusing to overwrite",
                None,
                None,
            )

        old_slug = None
        marker_path = clone_dir / MARKER_FILENAME
        if marker_path.exists():
            try:
                old_slug = str(
                    json.loads(marker_path.read_text(encoding="utf-8")).get(
                        "thread_slug"
                    )
                    or ""
                )
            except ValueError:
                old_slug = None

        entry = catalog.get(slug)
        _copy_tree_into(thread_dir, clone_dir)
        _refresh_marker(
            clone_dir,
            slug,
            entry.session_id if entry else "unknown",
            entry.content_hash if entry else "unknown",
        )

        message = f"{commit_message_prefix}: {slug}" + (
            f" (was {old_slug})" if old_slug and old_slug != slug else ""
        )
        new_sha, commit_err = _commit_all(clone_dir, message, run=run)
        if new_sha is None:
            return False, f"commit failed: {commit_err}", None, old_slug

        push = run(
            ["git", "-C", str(clone_dir), "push", "origin", "HEAD:main"],
            timeout=GIT_NET_TIMEOUT,
        )
        if not push.ok:
            return (
                False,
                f"push to {repo_name} rejected: {push.diagnostic()}",
                None,
                old_slug,
            )
        return True, f"pushed {new_sha[:12]} to {repo_name}", new_sha, old_slug
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def publish_thread(
    thread_dir: Path,
    slug: str,
    config: ThreadFactoryConfig,
    catalog: Catalog,
    *,
    run: RunFn = run_command,
) -> PublishResult:
    """Three exclusive paths: update an existing managed repo, create a new
    one (strictly under cap), or replace the least-active managed repo (at
    cap). `gh repo create` is reachable from exactly one of them, guarded by
    a count taken against the live account immediately beforehand."""
    owner = config.github_owner
    entry = catalog.get(slug)

    if config.dry_run:
        planned = _dry_run_plan(thread_dir, slug, config, catalog, run=run)
        for line in planned:
            print(f"[dry-run] would run: {line}")
        return PublishResult(
            slug=slug,
            repo_url=None,
            action="skipped",
            replaced_repo=None,
            pushed_ok=False,
            pushed_commit_sha=None,
            detail=f"dry run — {len(planned)} command(s) printed, none executed",
        )

    # Path 1: this thread already owns a repo -> update it in place
    if entry and entry.github_repo_url:
        repo_name = (
            entry.github_repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        )
        if is_thread_factory_repo(owner, repo_name, run=run):
            ok, detail, sha, _old = _push_into_existing_repo(
                thread_dir,
                slug,
                repo_name,
                config,
                catalog,
                expected_sha=entry.last_pushed_commit_sha,
                commit_message_prefix="Update thread-factory content",
                run=run,
            )
            return PublishResult(
                slug=slug,
                repo_url=entry.github_repo_url,
                action="updated" if ok else "skipped",
                replaced_repo=None,
                pushed_ok=ok,
                pushed_commit_sha=sha,
                detail=detail,
            )
        LOG.warning(
            "%s: catalog points at %s but it no longer carries %s — treating as unpublished",
            slug,
            repo_name,
            MARKER_FILENAME,
        )

    # Path 2: under cap -> create
    managed_count = count_live_managed_repos(owner, catalog, run=run)
    if managed_count < config.live_repo_cap:
        return _create_new_repo(
            thread_dir, slug, config, catalog, managed_count, run=run
        )

    # Path 3: at cap -> replace the oldest managed repo, never create
    LOG.info(
        "%s: at cap (%d/%d managed repos) — replacing the least-active managed repo",
        slug,
        managed_count,
        config.live_repo_cap,
    )
    repos = list_owner_repos(owner, run=run)
    attempted: list[str] = []
    remaining = [r for r in repos if r.is_thread_factory_managed]
    while True:
        target = pick_replacement_target(remaining)
        if target is None:
            return PublishResult(
                slug=slug,
                repo_url=None,
                action="skipped",
                replaced_repo=None,
                pushed_ok=False,
                pushed_commit_sha=None,
                detail=(
                    f"at cap ({managed_count}/{config.live_repo_cap}) and no managed repo "
                    f"could be safely replaced (tried: {', '.join(attempted) or 'none'}). "
                    "Thread left on disk, unpublished, and will be retried next run."
                ),
            )
        remaining = [r for r in remaining if r.name != target.name]
        attempted.append(target.name)

        # Live re-verification immediately before touching it -- the listing
        # above is already seconds stale, and a repo that stopped being
        # managed in that window is exactly the biovega case.
        if not is_thread_factory_repo(owner, target.name, run=run):
            LOG.warning(
                "%s lost its %s since listing — refusing to touch it, trying the next oldest",
                target.name,
                MARKER_FILENAME,
            )
            continue

        prior = next(
            (
                e
                for e in catalog.all_entries()
                if e.github_repo_url
                and e.github_repo_url.rstrip("/").endswith(f"/{target.name}")
            ),
            None,
        )
        ok, detail, sha, old_slug = _push_into_existing_repo(
            thread_dir,
            slug,
            target.name,
            config,
            catalog,
            expected_sha=prior.last_pushed_commit_sha if prior else None,
            commit_message_prefix=f"Replace with thread-factory content (inactive since {target.pushed_at})",
            run=run,
        )
        if not ok:
            LOG.warning(
                "replacement of %s failed (%s) — trying the next oldest",
                target.name,
                detail,
            )
            continue
        if prior:
            catalog.upsert(
                CatalogEntry(
                    **{
                        **asdict(prior),
                        "github_repo_url": None,
                        "last_pushed_commit_sha": None,
                        "last_processed_at": _utc_now(),
                    }
                )
            )
        return PublishResult(
            slug=slug,
            repo_url=target.url,
            action="replaced_inactive",
            replaced_repo=target.name,
            pushed_ok=True,
            pushed_commit_sha=sha,
            detail=f"{detail} (replaced {old_slug or target.name}, last pushed {target.pushed_at})",
        )


def _create_new_repo(
    thread_dir: Path,
    slug: str,
    config: ThreadFactoryConfig,
    catalog: Catalog,
    managed_count: int,
    *,
    run: RunFn = run_command,
) -> PublishResult:
    """The ONLY code path that calls `gh repo create`. Under-cap callers only."""
    owner = config.github_owner
    entry = catalog.get(slug)
    init = run(
        ["git", "-C", str(thread_dir), "rev-parse", "--git-dir"],
        timeout=GIT_LOCAL_TIMEOUT,
    )
    if not init.ok:
        created = run(
            ["git", "init", "-b", "main", str(thread_dir)], timeout=GIT_LOCAL_TIMEOUT
        )
        if not created.ok:
            return PublishResult(
                slug=slug,
                repo_url=None,
                action="skipped",
                replaced_repo=None,
                pushed_ok=False,
                pushed_commit_sha=None,
                detail=f"git init failed: {created.diagnostic()}",
            )
    _refresh_marker(
        thread_dir,
        slug,
        entry.session_id if entry else "unknown",
        entry.content_hash if entry else "unknown",
    )
    sha, commit_err = _commit_all(thread_dir, f"Thread Factory: {slug}", run=run)
    if sha is None:
        return PublishResult(
            slug=slug,
            repo_url=None,
            action="skipped",
            replaced_repo=None,
            pushed_ok=False,
            pushed_commit_sha=None,
            detail=f"commit failed before create: {commit_err}",
        )
    create = run(
        [
            "gh",
            "repo",
            "create",
            f"{owner}/{slug}",
            f"--{config.repo_visibility}",
            "--source",
            str(thread_dir),
            "--remote",
            "origin",
            "--push",
        ],
        timeout=GIT_NET_TIMEOUT,
    )
    if not create.ok:
        # Deliberately not retried: an ambiguous create failure that actually
        # succeeded would, on retry, either error or make a second repo and
        # silently consume another cap slot.
        return PublishResult(
            slug=slug,
            repo_url=None,
            action="skipped",
            replaced_repo=None,
            pushed_ok=False,
            pushed_commit_sha=None,
            detail=f"gh repo create failed (not retried — see module notes): {create.diagnostic()}",
        )
    return PublishResult(
        slug=slug,
        repo_url=f"https://github.com/{owner}/{slug}",
        action="created",
        replaced_repo=None,
        pushed_ok=True,
        pushed_commit_sha=sha,
        detail=f"created {owner}/{slug} at {sha[:12]} ({managed_count + 1}/{config.live_repo_cap} slots used)",
    )


def _dry_run_plan(
    thread_dir: Path,
    slug: str,
    config: ThreadFactoryConfig,
    catalog: Catalog,
    *,
    run: RunFn = run_command,
) -> list[str]:
    """Exactly the argv publishing WOULD run, built by the same branch logic
    as `publish_thread` using read-only calls only -- a dry run is a real
    rehearsal, not a separate description that can drift from what runs."""
    owner = config.github_owner
    entry = catalog.get(slug)
    managed_count = count_live_managed_repos(owner, catalog, run=run)
    if entry and entry.github_repo_url:
        name = entry.github_repo_url.rstrip("/").rsplit("/", 1)[-1]
        return [
            f"git clone --depth 1 https://github.com/{owner}/{name}.git <tmp>",
            f"git -C <tmp> rev-parse HEAD   # must equal {entry.last_pushed_commit_sha or '(none recorded)'}",
            "git -C <tmp> add -A",
            f"git -C <tmp> commit -m 'Update thread-factory content: {slug}'",
            "git -C <tmp> push origin HEAD:main",
        ]
    if managed_count < config.live_repo_cap:
        return [
            f"git init -b main {thread_dir}",
            f"git -C {thread_dir} add -A",
            f"git -C {thread_dir} commit -m 'Thread Factory: {slug}'",
            f"gh repo create {owner}/{slug} --{config.repo_visibility} "
            f"--source {thread_dir} --remote origin --push"
            f"   # {managed_count}/{config.live_repo_cap} slots used",
        ]
    target = pick_replacement_target(list_owner_repos(owner, run=run))
    name = target.name if target else "(no safe target found)"
    return [
        f"# AT CAP ({managed_count}/{config.live_repo_cap}) — would REPLACE, not create",
        f"gh api repos/{owner}/{name}/contents/{MARKER_FILENAME}   # live ownership re-check",
        f"git clone --depth 1 https://github.com/{owner}/{name}.git <tmp>",
        "git -C <tmp> rev-parse HEAD   # must match the catalog's recorded sha",
        f"git -C <tmp> commit -m 'Replace with thread-factory content: {slug}'",
        "git -C <tmp> push origin HEAD:main   # never --force",
    ]


def publish_phase(
    ready: list[tuple[ThreadCandidate, TriageVerdict, Path]],
    config: ThreadFactoryConfig,
    catalog: Catalog,
    *,
    run: RunFn = run_command,
) -> list[PublishResult]:
    """Publish every ready thread, saving the catalog after each one."""
    ok, detail = check_publish_preconditions(config, run=run)
    if not ok:
        raise ConfigError(f"publish preconditions failed: {detail}")
    LOG.info("publish: %s", detail)

    results: list[PublishResult] = []
    for candidate, verdict, thread_dir in ready:
        result = publish_thread(thread_dir, candidate.slug, config, catalog, run=run)
        LOG.info("publish %s -> %s: %s", candidate.slug, result.action, result.detail)
        catalog.upsert(
            _entry_for(
                candidate,
                verdict,
                github_repo_url=result.repo_url,
                last_pushed_commit_sha=result.pushed_commit_sha,
                last_pushed_at=_utc_now() if result.pushed_ok else None,
            )
        )
        catalog.save()
        results.append(result)
    return results


def prune_local_thread(
    thread_dir: Path,
    slug: str,
    catalog: Catalog,
    config: ThreadFactoryConfig,
    *,
    run: RunFn = run_command,
) -> tuple[bool, str]:
    """Two confirmations before one irreversible delete: publish already
    reported pushed_ok, and this independently re-verifies against the live
    remote. On failure: thread_dir is left exactly as-is, no auto-retry
    (retrying a repo that will never verify just re-pushes broken content),
    surfaced in the audit report instead. Never deletes the session backup
    or the catalog row."""
    entry = catalog.get(slug)
    if entry is None:
        return (
            False,
            f"{slug}: no catalog entry — refusing to prune an unrecorded thread",
        )
    if not entry.github_repo_url or not entry.last_pushed_commit_sha:
        return (
            False,
            f"{slug}: never published (no repo url or commit sha) — nothing to prune to",
        )

    repo_name = (
        entry.github_repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    )
    verified, detail = verify_pushed_and_retrievable(
        config.github_owner, repo_name, entry.last_pushed_commit_sha, run=run
    )
    if not verified:
        return False, f"{slug}: NOT pruned — independent verification failed: {detail}"

    backup = (
        config.threads_dir
        / "_extracted"
        / "_sessions-backup"
        / f"{entry.session_id}.json"
    )
    if not backup.exists():
        return False, f"{slug}: NOT pruned — the session backup {backup} is missing"
    if not thread_dir.exists():
        catalog.upsert(
            CatalogEntry(
                **{
                    **asdict(entry),
                    "pruned_locally": True,
                    "last_processed_at": _utc_now(),
                }
            )
        )
        return True, f"{slug}: already absent from disk; catalog marked pruned"

    try:
        shutil.rmtree(thread_dir)
    except OSError as e:
        return False, f"{slug}: NOT pruned — rmtree({thread_dir}) failed: {e}"

    catalog.upsert(
        CatalogEntry(
            **{**asdict(entry), "pruned_locally": True, "last_processed_at": _utc_now()}
        )
    )
    catalog.save()
    return True, f"{slug}: pruned {thread_dir} after {detail}"


def restore_thread(
    slug: str,
    catalog: Catalog,
    config: ThreadFactoryConfig,
    *,
    run: RunFn = run_command,
) -> Path:
    """Clone a pruned thread back to disk -- the other half of pruning.
    Drops `.git` afterward so the restored tree is a plain working
    directory; publishing re-clones into a temp dir anyway."""
    entry = catalog.get(slug)
    if entry is None:
        raise ThreadFactoryError(f"cannot restore {slug!r}: not in the catalog")
    if not entry.github_repo_url:
        raise ThreadFactoryError(f"cannot restore {slug!r}: it was never published")

    target = config.threads_dir / slug
    if target.exists():
        LOG.info("restore %s: already on disk at %s", slug, target)
        return target

    repo_name = (
        entry.github_repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    )
    url = f"https://github.com/{config.github_owner}/{repo_name}.git"
    target.parent.mkdir(parents=True, exist_ok=True)
    clone = run(
        ["git", "clone", "--depth", "1", url, str(target)], timeout=GIT_NET_TIMEOUT
    )
    if not clone.ok:
        raise ThreadFactoryError(
            f"cannot restore {slug!r} from {url}: {clone.diagnostic()}"
        )
    shutil.rmtree(target / ".git", ignore_errors=True)
    catalog.upsert(
        CatalogEntry(
            **{
                **asdict(entry),
                "pruned_locally": False,
                "last_processed_at": _utc_now(),
            }
        )
    )
    catalog.save()
    LOG.info("restore %s: cloned %s to %s", slug, url, target)
    return target


def audit_thread(
    entry: CatalogEntry,
    thread_dir: Path | None,
    config: ThreadFactoryConfig,
    *,
    run: RunFn = run_command,
) -> AuditResult:
    """Prove, live, that one catalog row still describes reality. Works
    against the remote + catalog only -- for a pruned thread local content is
    gone by design. Never a hardcoded PASS."""
    checks: dict[str, bool] = {}
    detail: list[str] = []

    if entry.verdict in ("ARCHIVE", "DELETE", "MERGE"):
        checks["not_expected_to_publish"] = True
        detail.append(f"{entry.verdict} threads are never published; nothing to verify")
        return AuditResult(slug=entry.slug, checks=checks, passed=True, detail=detail)

    checks["has_repo_url"] = bool(entry.github_repo_url)
    checks["has_commit_sha"] = bool(entry.last_pushed_commit_sha)
    if not (entry.github_repo_url and entry.last_pushed_commit_sha):
        detail.append("catalog row is LIVE but has no repo url and/or no commit sha")
        return AuditResult(slug=entry.slug, checks=checks, passed=False, detail=detail)

    repo_name = (
        entry.github_repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    )
    owner = config.github_owner

    verified, verify_detail = verify_pushed_and_retrievable(
        owner, repo_name, entry.last_pushed_commit_sha, run=run
    )
    checks["remote_head_and_manifest"] = verified
    detail.append(verify_detail)

    checks["marker_present"] = is_thread_factory_repo(owner, repo_name, run=run)
    if not checks["marker_present"]:
        detail.append(
            f"{repo_name} no longer carries {MARKER_FILENAME} — this repo is no longer ours"
        )

    readme = run_read_command(
        [
            "gh",
            "api",
            f"repos/{owner}/{repo_name}/contents/README.md",
            "-H",
            "Accept: application/vnd.github.raw",
        ],
        timeout=GH_TIMEOUT,
        run=run,
    )
    checks["readme_fetchable"] = readme.ok and bool(readme.stdout.strip())
    if checks["readme_fetchable"]:
        clean = not is_placeholder(readme.stdout, min_chars=MIN_FIELD_CHARS)
        checks["remote_content_not_placeholder"] = clean
        if not clean:
            detail.append("published README.md matches the placeholder heuristic")
    else:
        checks["remote_content_not_placeholder"] = False
        detail.append(f"README.md not fetchable: {readme.diagnostic()}")

    on_disk = bool(thread_dir and thread_dir.exists())
    checks["prune_state_consistent"] = (not on_disk) if entry.pruned_locally else True
    if entry.pruned_locally and on_disk:
        detail.append(f"catalog says pruned but {thread_dir} still exists")
    elif not entry.pruned_locally and not on_disk:
        detail.append(
            "not marked pruned and not on disk (deleted outside thread-factory?)"
        )

    return AuditResult(
        slug=entry.slug, checks=checks, passed=all(checks.values()), detail=detail
    )


def audit_phase(
    catalog: Catalog, config: ThreadFactoryConfig, *, run: RunFn = run_command
) -> list[AuditResult]:
    """Audit every catalog row. Standalone — needs no prior phase in this run."""
    results: list[AuditResult] = []
    for entry in catalog.all_entries():
        thread_dir = config.threads_dir / entry.slug
        results.append(
            audit_thread(
                entry, thread_dir if thread_dir.exists() else None, config, run=run
            )
        )
    failed = [r for r in results if not r.passed]
    LOG.info("audit: %d entries, %d failing", len(results), len(failed))
    return results


def write_audit_report(results: list[AuditResult], report_path: Path) -> None:
    """Failures first — nobody scrolls past 40 PASS lines to find the one row
    that says content was deleted after a push that never landed."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(results, key=lambda r: (r.passed, r.slug))
    passed = sum(1 for r in results if r.passed)
    lines = [
        "# Thread Factory audit",
        "",
        f"_Generated {_utc_now()} by thread_factory.py_",
        "",
        f"**{passed} / {len(results)} passed.**"
        + ("" if passed == len(results) else "  Failures are listed first."),
        "",
    ]
    for result in ordered:
        lines.append(f"## {'PASS' if result.passed else 'FAIL'} — {result.slug}")
        lines.append("")
        for name, value in sorted(result.checks.items()):
            lines.append(f"- `{name}`: {'ok' if value else '**FAILED**'}")
        for note in result.detail:
            lines.append(f"- {note}")
        lines.append("")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOG.info("audit report written to %s", report_path)


EXIT_OK = 0
EXIT_WORK_FAILED = 1
EXIT_PRECONDITION = 2


def run_pipeline(
    config: ThreadFactoryConfig,
    catalog: Catalog,
    llm_call: LLMCallFn | None = None,
    *,
    run: RunFn = run_command,
) -> dict[str, Any]:
    """Extract -> triage/structure -> publish -> prune, ONE THREAD AT A TIME,
    with catalog.save() after each row. A crash leaves a catalog that
    accurately says which threads finished and which never started."""
    started = time.time()
    summary: dict[str, Any] = {
        "started_at": _utc_now(),
        "candidates": 0,
        "published": [],
        "pruned": [],
        "failures": [],
        "audit_failures": [],
        "dry_run": config.dry_run,
    }

    if not config.dry_run:
        ok, detail = check_publish_preconditions(config, run=run)
        if not ok:
            raise ConfigError(detail)
        LOG.info("preconditions: %s", detail)

    triage_call = llm_call or make_llm_call(config, phase="triage")
    distill_call = llm_call or make_llm_call(config, phase="distill")

    candidates = extract_phase(config, catalog)
    summary["candidates"] = len(candidates)

    for candidate in candidates:
        ready, diagnostics = structure_phase(
            [candidate], config, catalog, _phased(triage_call, distill_call)
        )
        summary["failures"].extend(diagnostics)
        if not ready:
            continue
        _cand, verdict, thread_dir = ready[0]

        result = publish_thread(thread_dir, candidate.slug, config, catalog, run=run)
        catalog.upsert(
            _entry_for(
                candidate,
                verdict,
                github_repo_url=result.repo_url,
                last_pushed_commit_sha=result.pushed_commit_sha,
                last_pushed_at=_utc_now() if result.pushed_ok else None,
            )
        )
        catalog.save()
        summary["published"].append(asdict(result))
        if not result.pushed_ok:
            if not config.dry_run:
                summary["failures"].append(
                    f"{candidate.slug}: publish failed: {result.detail}"
                )
            continue

        pruned, prune_detail = prune_local_thread(
            thread_dir, candidate.slug, catalog, config, run=run
        )
        summary["pruned"].append(
            {"slug": candidate.slug, "pruned": pruned, "detail": prune_detail}
        )
        if not pruned:
            summary["failures"].append(prune_detail)

    report_path = (
        config.threads_dir
        / "_audit"
        / f"audit-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.md"
    )
    audit_results = audit_phase(catalog, config, run=run) if not config.dry_run else []
    if audit_results:
        write_audit_report(audit_results, report_path)
        summary["audit_report"] = str(report_path)
        summary["audit_failures"] = [r.slug for r in audit_results if not r.passed]

    summary["elapsed_s"] = round(time.time() - started, 1)
    summary["finished_at"] = _utc_now()
    return summary


def _phased(triage_call: LLMCallFn, distill_call: LLMCallFn) -> LLMCallFn:
    """Route by task_type so triage and distill can use different models
    through the single callback structure_phase's signature commits to."""

    def _call(task_type: str, system: str, user: str) -> str:
        return (triage_call if task_type.endswith("triage") else distill_call)(
            task_type, system, user
        )

    return _call


def import_legacy(
    config: ThreadFactoryConfig, catalog: Catalog, *, run: RunFn = run_command
) -> list[str]:
    """Backfill catalog rows from pre-catalog ~/threads/*/ directories.
    Explicit, run-once, never part of `run`. Only imports a directory whose
    marker names a repo that STILL exists and STILL carries the marker --
    an unverified import would tell the cap-replace path it owns a repo it
    may not."""
    imported: list[str] = []
    if not config.threads_dir.is_dir():
        LOG.error("import-legacy: %s is not a directory", config.threads_dir)
        return imported

    for thread_dir in sorted(config.threads_dir.iterdir()):
        if not thread_dir.is_dir() or thread_dir.name.startswith("_"):
            continue
        slug = thread_dir.name
        if catalog.get(slug):
            continue
        marker = thread_dir / MARKER_FILENAME
        if not marker.exists():
            LOG.info("import-legacy: %s has no %s — skipping", slug, MARKER_FILENAME)
            continue
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            LOG.warning("import-legacy: %s has an unreadable marker: %s", slug, e)
            continue
        if not is_thread_factory_repo(config.github_owner, slug, run=run):
            LOG.info(
                "import-legacy: %s/%s is not a live managed repo — skipping",
                config.github_owner,
                slug,
            )
            continue
        remote = run_read_command(
            [
                "git",
                "ls-remote",
                f"https://github.com/{config.github_owner}/{slug}.git",
                "HEAD",
            ],
            timeout=GIT_NET_TIMEOUT,
            run=run,
        )
        sha = remote.stdout.split()[0] if remote.ok and remote.stdout.split() else None
        catalog.upsert(
            CatalogEntry(
                slug=slug,
                session_id=str(payload.get("session_id") or "unknown"),
                content_hash=str(payload.get("content_hash") or ""),
                verdict="LIVE",
                github_repo_url=f"https://github.com/{config.github_owner}/{slug}",
                last_pushed_commit_sha=sha,
                last_pushed_at=str(payload.get("pushed_at") or ""),
                last_processed_at=_utc_now(),
                pruned_locally=False,
            )
        )
        catalog.save()
        imported.append(slug)
        LOG.info("import-legacy: imported %s (HEAD %s)", slug, (sha or "unknown")[:12])
    return imported


def main(argv: list[str] | None = None) -> int:
    """Exit codes are a contract with the systemd unit:
      0  everything attempted succeeded or was skipped by design.
      1  at least one LIVE thread's publish or prune failed verification --
         left un-pruned and on disk, named in the audit report.
      2  a precondition failed before any work started (bad config, no gh
         auth, unreadable sessions_dir).
    `thread_factory_bimonthly.sh` treats only 0 as success."""
    parser = argparse.ArgumentParser(
        prog="thread_factory.py",
        description="Curate AI chat sessions into published, pruned, audited repos.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"config JSON (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print every git/gh command instead of running it",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("run", "the full pipeline: extract, structure, publish, prune, audit"),
        ("extract", "phase 1 only: back up sessions and list changed candidates"),
        ("structure", "phases 1-2: triage and write thread folders, no publishing"),
        ("publish", "phase 3 only: publish thread folders already on disk"),
        ("audit", "phase 4 only: re-prove every catalog row against the remote"),
        ("import-legacy", "backfill catalog rows from pre-catalog ~/threads dirs"),
    ):
        sub.add_parser(name, help=help_text)
    restore = sub.add_parser("restore", help="clone a pruned thread back to disk")
    restore.add_argument("slug", help="the thread slug to restore")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_PRECONDITION
    if args.dry_run:
        config = ThreadFactoryConfig(**{**asdict(config), "dry_run": True})

    catalog = Catalog(config.catalog_path)
    catalog.load()

    try:
        if args.command == "run":
            summary = run_pipeline(config, catalog)
            print(json.dumps(summary, indent=2, default=str))
            return (
                EXIT_WORK_FAILED
                if (summary["failures"] or summary["audit_failures"])
                else EXIT_OK
            )

        if args.command == "extract":
            for candidate in extract_phase(config, catalog):
                print(
                    f"{candidate.slug}\t{candidate.message_count} msgs\t{candidate.content_hash[:12]}"
                )
            return EXIT_OK

        if args.command == "structure":
            candidates = extract_phase(config, catalog)
            ready, diagnostics = structure_phase(
                candidates,
                config,
                catalog,
                _phased(
                    make_llm_call(config, phase="triage"),
                    make_llm_call(config, phase="distill"),
                ),
            )
            for _c, verdict, thread_dir in ready:
                print(f"{verdict.verdict}\t{thread_dir}")
            for line in diagnostics:
                print(f"FAILED\t{line}", file=sys.stderr)
            return EXIT_WORK_FAILED if diagnostics else EXIT_OK

        if args.command == "publish":
            pending = [
                (
                    ThreadCandidate(
                        e.session_id, e.slug, e.slug, 0, "", e.content_hash
                    ),
                    TriageVerdict(e.session_id, "LIVE", "from catalog", None, 1.0),
                    config.threads_dir / e.slug,
                )
                for e in catalog.all_entries()
                if e.verdict == "LIVE"
                and not e.pruned_locally
                and (config.threads_dir / e.slug).is_dir()
            ]
            publish_results = publish_phase(pending, config, catalog)
            for result in publish_results:
                print(f"{result.action}\t{result.slug}\t{result.detail}")
            return (
                EXIT_WORK_FAILED
                if any(
                    not r.pushed_ok and r.action != "skipped" for r in publish_results
                )
                else EXIT_OK
            )

        if args.command == "audit":
            audit_results = audit_phase(catalog, config)
            report = (
                config.threads_dir
                / "_audit"
                / f"audit-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.md"
            )
            write_audit_report(audit_results, report)
            for audit_result in audit_results:
                print(
                    f"{'PASS' if audit_result.passed else 'FAIL'}\t{audit_result.slug}"
                )
            print(f"\nreport: {report}")
            return (
                EXIT_WORK_FAILED
                if any(not r.passed for r in audit_results)
                else EXIT_OK
            )

        if args.command == "restore":
            print(restore_thread(args.slug, catalog, config))
            return EXIT_OK

        if args.command == "import-legacy":
            imported = import_legacy(config, catalog)
            print(
                f"imported {len(imported)} legacy thread(s): {', '.join(imported) or '(none)'}"
            )
            return EXIT_OK

    except ConfigError as e:
        print(f"precondition failed: {e}", file=sys.stderr)
        return EXIT_PRECONDITION
    except ThreadFactoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_WORK_FAILED
    except KeyboardInterrupt:
        print(
            "\ninterrupted — the catalog reflects every thread that finished",
            file=sys.stderr,
        )
        return EXIT_WORK_FAILED

    parser.error(f"unhandled command {args.command!r}")
    return EXIT_PRECONDITION


if __name__ == "__main__":
    sys.exit(main())
