"""Sensei skill marketplace — source catalog, browse, audit-before-install.

2026-09-01. Claude's half of the Skill Marketplace / Learning Loop split
(Elijah's explicit priority call, see ROADMAP.md Phase 3.3b). Hermes
builds the `skill browse/install/audit` REPL commands in master_ai.py on
top of this module, mirroring the `mcp` command family's split against
sensei_mcp_client.py exactly:
  - this module owns state (a JSON catalog) + the probe/audit logic
  - master_ai.py's REPL commands are thin dispatch wrappers

Conventions follow sensei_mcp_client.py (read before writing this):
  - State is a flat JSON file, ~/.master_ai_skill_sources.json, same
    atomic tmp+rename + chmod 0600 hygiene.
  - stdlib only: json, os, re, shutil, time, dataclasses, pathlib.
  - Probe-before-trust: install() always audits first and refuses to
    stage anything that fails. Nothing here executes third-party code —
    browse/audit are pure read + static-analysis.

What "install" means here (deliberately narrow, per the 2026-09-01
portability research in ROADMAP.md Phase 3.3): SKILL.md's frontmatter
format is already portable across Claude/Hermes/OpenClaw at the spec
level, but Sensei's typed STEPS execution model is NOT a drop-in target
for a raw prose skill — that's a safety tradeoff, not a limitation
(skill_runtime.py requires a hand-written state machine rather than
letting a model improvise tool calls from markdown). So `install_skill()`
audits a source skill and, if it passes, stages its raw files under
`~/.master_ai_skills/_staging/<name>/` — it does NOT claim the skill is
runnable. A staged skill still needs hand-adaptation to `recipe.py` +
`STEPS` before `skill_runtime.run_skill()` can use it, exactly like the
4 skills already adapted (google-workspace, web-search-ddgr,
codebase-inspection, systematic-debugging).

The harder audit case is the ALREADY-ADAPTED skill: `audit_skill()` run
against something under `~/.master_ai_skills/<name>/` (not `_staging`)
statically scans `recipe.py` for the exact sandbox-bypass bug class the
Phase 3.3 fix found and closed (direct `subprocess`/`os.system` calls
that skip `sandbox.py`) — and HARD FAILS on that, not just a warning,
because that skill is actually executable by `skill_runtime.run_skill()`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from skill_runtime import SKILLS_ROOT  # ~/.master_ai_skills — single source of truth

CATALOG_PATH = Path.home() / ".master_ai_skill_sources.json"
STAGING_ROOT = SKILLS_ROOT / "_staging"
LOG_PATH = SKILLS_ROOT / "marketplace.log"

# Source #1 is pre-registered: Hermes's own skill library. Already local,
# already known-portable at the SKILL.md frontmatter level (2026-09-01
# research: pulled a real skill from github.com/openclaw/agent-skills and
# confirmed identical name/description YAML-frontmatter shape).
DEFAULT_SOURCES = {
    "hermes": {"path": str(Path.home() / ".hermes" / "skills"), "kind": "dir"},
}

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FM_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$", re.MULTILINE)

# ── Static-audit patterns ────────────────────────────────────────────
# Sandbox-bypass: a direct call to subprocess/os.system without the file
# also importing sandbox.py's wrapper. Same bug class as commit 4083188.
_BYPASS_CALL_RE = re.compile(
    r"\b(?:subprocess\.(?:run|Popen|call|check_call|check_output)|os\.system)\s*\("
)
_SANDBOX_IMPORT_RE = re.compile(r"^\s*(?:import\s+sandbox\b|from\s+sandbox\s+import\b)", re.MULTILINE)
_EVAL_EXEC_RE = re.compile(r"\b(?:eval|exec)\s*\(")
_PIPE_TO_SHELL_RE = re.compile(r"curl\s+[^\n|]*\|\s*(?:bash|sh)\b")
_PICKLE_LOADS_RE = re.compile(r"\bpickle\.loads\s*\(")
_HARDCODED_SECRET_RE = re.compile(
    r"(?:api[_-]?key|secret|token|password)\s*=\s*[\"'][A-Za-z0-9_\-]{20,}[\"']", re.IGNORECASE
)


def _log(msg: str) -> None:
    try:
        SKILLS_ROOT.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ─── Source catalog storage (mirrors sensei_mcp_client._load/_save_catalog) ──

def _load_catalog() -> dict:
    try:
        if not CATALOG_PATH.exists():
            return {"version": 1, "sources": dict(DEFAULT_SOURCES)}
        data = json.loads(CATALOG_PATH.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
            return {"version": 1, "sources": dict(DEFAULT_SOURCES)}
        data.setdefault("version", 1)
        # Always ensure the built-in default source is present even if the
        # on-disk catalog predates it or was hand-edited.
        for k, v in DEFAULT_SOURCES.items():
            data["sources"].setdefault(k, v)
        return data
    except Exception as e:
        _log(f"CATALOG_LOAD_ERROR: {e}")
        return {"version": 1, "sources": dict(DEFAULT_SOURCES)}


def _save_catalog(cat: dict) -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CATALOG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cat, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CATALOG_PATH)


def list_sources() -> dict:
    return _load_catalog()["sources"]


def add_source(name: str, path: str) -> None:
    p = Path(os.path.expanduser(path))
    if not p.is_dir():
        raise NotADirectoryError(f"no such directory: {p}")
    cat = _load_catalog()
    cat["sources"][name] = {"path": str(p), "kind": "dir"}
    _save_catalog(cat)


def remove_source(name: str) -> None:
    cat = _load_catalog()
    cat["sources"].pop(name, None)
    _save_catalog(cat)


def _source_path(source_name: str) -> Path:
    sources = list_sources()
    entry = sources.get(source_name)
    if entry is None:
        raise KeyError(f"unknown source: {source_name!r} (known: {sorted(sources)})")
    return Path(entry["path"])


# ─── Frontmatter parsing (stdlib-only, no PyYAML dependency) ─────────

def _parse_frontmatter(text: str) -> dict:
    """Extract simple `key: value` pairs from a leading `---` YAML block.
    Not a real YAML parser — this codebase has no PyYAML dependency and
    every frontmatter block observed in ~/.hermes/skills/ is flat
    scalars, no nesting needed for name/description/version/author."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    block = m.group(1)
    out = {}
    for fm in _FM_FIELD_RE.finditer(block):
        key, val = fm.group(1), fm.group(2)
        val = val.strip().strip('"').strip("'")
        out[key] = val
    return out


def _fallback_description(text: str) -> str:
    """No frontmatter (e.g. our own adapted skills' SKILL.md files use
    plain prose, no YAML block) — use the first heading + first paragraph
    line instead, truncated."""
    lines = [l.strip() for l in text.splitlines()]
    heading = next((l.lstrip("#").strip() for l in lines if l.startswith("#")), "")
    body = next((l for l in lines if l and not l.startswith("#")), "")
    combined = " — ".join(x for x in (heading, body) if x)
    return combined[:200]


# ─── Browse ───────────────────────────────────────────────────────────

def _adapted_skill_names() -> set:
    if not SKILLS_ROOT.is_dir():
        return set()
    return {
        d.name for d in SKILLS_ROOT.iterdir()
        if d.is_dir() and d.name != "_staging" and (d / "recipe.py").exists()
    }


def browse_source(source_name: Optional[str] = None) -> list:
    """List skills available under a registered source. Read-only —
    installs nothing, executes nothing. Returns a list of dicts:
    {id, name, description, path, adapted}. `id` is the path relative to
    the source root (e.g. "research/web-search-ddgr") since the same
    leaf name can repeat across categories."""
    sources = list_sources()
    if source_name is None:
        if len(sources) != 1:
            raise ValueError(
                f"source name required when multiple sources are registered: {sorted(sources)}"
            )
        source_name = next(iter(sources))

    root = _source_path(source_name)
    if not root.is_dir():
        raise NotADirectoryError(f"source {source_name!r} path does not exist: {root}")

    adapted = _adapted_skill_names()
    out = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        rel_id = str(skill_dir.relative_to(root))
        try:
            text = skill_md.read_text(errors="replace")
        except OSError as e:
            _log(f"BROWSE_READ_ERROR {skill_md}: {e}")
            continue
        fm = _parse_frontmatter(text)
        name = fm.get("name") or skill_dir.name
        description = fm.get("description") or _fallback_description(text)
        out.append({
            "id": rel_id,
            "name": name,
            "description": description,
            "path": str(skill_dir),
            "adapted": skill_dir.name in adapted,
        })
    return out


# ─── Audit ──────────────────────────────────────────────────────────

@dataclass
class AuditResult:
    passed: bool
    reasons: list = field(default_factory=list)   # hard-fail causes
    warnings: list = field(default_factory=list)   # informational, non-blocking
    scanned_files: int = 0
    has_recipe: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _scan_file_for_findings(path: Path, text: str, is_recipe: bool) -> tuple:
    """Returns (bypass_findings, other_findings) as lists of
    'path:line: pattern' strings."""
    bypass, other = [], []
    lines = text.splitlines()
    has_sandbox_import = bool(_SANDBOX_IMPORT_RE.search(text))

    for i, line in enumerate(lines, 1):
        if _BYPASS_CALL_RE.search(line) and not has_sandbox_import:
            bypass.append(f"{path}:{i}: direct subprocess/os.system call without `import sandbox`")
        if _EVAL_EXEC_RE.search(line):
            (bypass if is_recipe else other).append(f"{path}:{i}: eval()/exec() call")
        if _PIPE_TO_SHELL_RE.search(line):
            other.append(f"{path}:{i}: curl-pipe-to-shell pattern")
        if _PICKLE_LOADS_RE.search(line):
            other.append(f"{path}:{i}: pickle.loads() (unsafe deserialization)")
        if _HARDCODED_SECRET_RE.search(line):
            other.append(f"{path}:{i}: hardcoded-looking secret/token literal")
    return bypass, other


def _audit_dir(skill_dir: Path) -> AuditResult:
    if not (skill_dir / "SKILL.md").exists():
        return AuditResult(passed=False, reasons=[f"no SKILL.md at {skill_dir}"])

    recipe_path = skill_dir / "recipe.py"
    has_recipe = recipe_path.exists()

    reasons, warnings = [], []
    scanned = 0
    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix not in (".py", ".sh"):
            continue
        if any(part in ("sessions", "knowledge", "__pycache__") for part in f.parts):
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        scanned += 1
        is_recipe = f == recipe_path
        bypass, other = _scan_file_for_findings(f, text, is_recipe)
        if is_recipe:
            # Executable by skill_runtime.run_skill() today — bypass/eval
            # findings here are a hard fail, not a warning.
            reasons.extend(bypass)
            warnings.extend(other)
        else:
            # Not directly executed by our runtime (either a non-recipe
            # helper script, or the whole skill isn't adapted yet) —
            # informational only.
            warnings.extend(bypass)
            warnings.extend(other)

    passed = not reasons
    if not has_recipe and passed:
        warnings.append(
            "no recipe.py — not yet adapted to skill_runtime STEPS format; "
            "audit passing means safe to stage, not runnable"
        )
    return AuditResult(passed=passed, reasons=reasons, warnings=warnings,
                        scanned_files=scanned, has_recipe=has_recipe)


def audit_skill(source_name: str, skill_id: str) -> AuditResult:
    """Audit a skill under a registered source before install. skill_id
    is the `id` field from browse_source() (path relative to source root)."""
    root = _source_path(source_name)
    skill_dir = root / skill_id
    if not skill_dir.is_dir():
        return AuditResult(passed=False, reasons=[f"no such skill dir: {skill_dir}"])
    result = _audit_dir(skill_dir)
    _log(f"AUDIT {source_name}/{skill_id}: passed={result.passed} "
         f"reasons={len(result.reasons)} warnings={len(result.warnings)}")
    return result


def audit_adapted_skill(name: str) -> AuditResult:
    """Re-audit an already-adapted skill under ~/.master_ai_skills/<name>/
    (not staging). Used by the `skill audit <name>` REPL command."""
    skill_dir = SKILLS_ROOT / name
    if not skill_dir.is_dir():
        return AuditResult(passed=False, reasons=[f"no adapted skill at {skill_dir}"])
    result = _audit_dir(skill_dir)
    _log(f"AUDIT_ADAPTED {name}: passed={result.passed} reasons={len(result.reasons)}")
    return result


# ─── Install (audit-gated staging, never a live drop-in) ─────────────

def install_skill(source_name: str, skill_id: str) -> dict:
    """Audit first; refuse to copy anything that fails. On pass, copies
    the raw source files into ~/.master_ai_skills/_staging/<leaf-name>/
    and returns guidance — never claims the staged skill is runnable."""
    audit = audit_skill(source_name, skill_id)
    if not audit.passed:
        return {"staged": False, "audit": audit.to_dict(), "path": None}

    root = _source_path(source_name)
    src_dir = root / skill_id
    leaf_name = src_dir.name
    dest_dir = STAGING_ROOT / leaf_name

    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    shutil.copytree(src_dir, dest_dir)

    _log(f"INSTALL {source_name}/{skill_id} -> {dest_dir}")
    return {
        "staged": True,
        "audit": audit.to_dict(),
        "path": str(dest_dir),
        "note": (
            "audit passed and files are staged — this still needs hand "
            "adaptation to the recipe.py/STEPS contract before "
            "skill_runtime.run_skill() can use it (format-portable does "
            "not mean execution-model-portable; see skill_runtime.py "
            "docstring)."
        ),
    }
