"""Typed action envelope for Master AI directives (P0.4).

The legacy executor in master_ai.process_reply() parses ``RUN:``/``READ:``/
``CREATE:``/``EDIT:``/``RUNTERM:`` lines with regex and dispatches them
directly. That works, but the agent-standards report has flagged it as a
WARN: there is no typed boundary between "parsed text" and "action about
to run." Hooks (P1.4), subagents (P1.5), the observability dashboard
(P1.7), and the eventual sandbox layer (P2) all need a stable schema to
hang off.

This module provides that schema. It does NOT replace the legacy parser;
the existing executor still works on raw text. typed_actions.py is a
SUPERSET — callers that want structured access (audit jsonl, hooks,
subagents) build TypedAction objects via :func:`parse_directive` or
:func:`make_audit_record`. The legacy text path continues to operate
unchanged behind it.

Public API:

    TypedAction               — dataclass with the full lifecycle fields
    Kind, Risk, Status        — enum-like string constants
    parse_directive(line, …)  — single-line parser → TypedAction or None
    parse_reply(text, …)      — full reply parser → list[TypedAction]
    classify_risk(action)     — set/return action.risk from heuristics
    make_audit_record(...)    — snapshot dict suitable for jsonl writing
    audit_outcome_from_kind() — map legacy audit kinds → outcome string
    DIRECTIVE_KINDS           — frozenset of recognized kind tokens

No master_ai or router imports here — typed_actions stays standalone so
tests can import it without any side effects, and so hooks/subagents can
depend on it without pulling in the orchestrator.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


class Kind:
    RUN = "RUN"
    RUNTERM = "RUNTERM"
    READ = "READ"
    CREATE = "CREATE"
    EDIT = "EDIT"
    REMEMBER = "REMEMBER"  # 2026-05-11: model self-write to memory
    # 2026-08-23: kinds the legacy parser added after this module's initial
    # build (2026-05-17) that typed_actions had drifted out of sync with.
    PLAN = "PLAN"
    DONE = "DONE"
    THINK = "THINK"
    RUN_SKILL = "RUN_SKILL"
    SEND_EMAIL = "SEND_EMAIL"
    # 2026-05-12: Chrome extension M1 surface. Browser-side execution only —
    # backend proposes, extension dispatches via content script, posts result
    # to /extension/action_result. Backend never reaches into the DOM directly.
    BROWSER_CLICK = "BROWSER_CLICK"
    BROWSER_FILL = "BROWSER_FILL"
    BROWSER_FILL_FORM = "BROWSER_FILL_FORM"
    BROWSER_UPLOAD_FILE = "BROWSER_UPLOAD_FILE"
    BROWSER_SUBMIT = "BROWSER_SUBMIT"
    BROWSER_READ = "BROWSER_READ"
    BROWSER_READ_PAGE = "BROWSER_READ_PAGE"
    BROWSER_READ_PAGE_FULL = "BROWSER_READ_PAGE_FULL"
    BROWSER_OBSERVE = "BROWSER_OBSERVE"
    BROWSER_NAV = "BROWSER_NAV"
    BROWSER_CLOSE_TAB = "BROWSER_CLOSE_TAB"
    BROWSER_SCREENSHOT = "BROWSER_SCREENSHOT"
    BROWSER_WAIT = "BROWSER_WAIT"
    BROWSER_SCROLL = "BROWSER_SCROLL"
    BROWSER_DOUBLE_CLICK = "BROWSER_DOUBLE_CLICK"
    BROWSER_FIND = "BROWSER_FIND"
    BROWSER_EXTRACT_LIST = "BROWSER_EXTRACT_LIST"
    BROWSER_DRIVE_INSPECT_FOLDER = "BROWSER_DRIVE_INSPECT_FOLDER"
    BROWSER_CDP_MOUSE = "BROWSER_CDP_MOUSE"
    BROWSER_CDP_KEY = "BROWSER_CDP_KEY"
    BROWSER_TAB_CREATE = "BROWSER_TAB_CREATE"
    REMOTE_MCP = "REMOTE_MCP"


class Risk:
    SAFE = "safe"       # READ, harmless RUN (ls, cat, file existence checks)
    NORMAL = "normal"   # RUN/RUNTERM/CREATE/EDIT with side effects
    HIGH = "high"       # destructive RUN (rm -rf, dd, mkfs, chmod -R 777, etc.)
    BLOCKED = "blocked" # safeguard refused; never executes


class Status:
    PARSED = "parsed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


DIRECTIVE_KINDS = frozenset({Kind.RUN, Kind.RUNTERM, Kind.READ, Kind.CREATE, Kind.EDIT, Kind.REMEMBER,
                             Kind.PLAN, Kind.DONE, Kind.THINK, Kind.RUN_SKILL, Kind.SEND_EMAIL,
                             Kind.BROWSER_CLICK, Kind.BROWSER_FILL, Kind.BROWSER_FILL_FORM, Kind.BROWSER_UPLOAD_FILE,
                             Kind.BROWSER_SUBMIT, Kind.BROWSER_READ,
                             Kind.BROWSER_READ_PAGE, Kind.BROWSER_READ_PAGE_FULL, Kind.BROWSER_OBSERVE, Kind.BROWSER_NAV,
                             Kind.BROWSER_CLOSE_TAB, Kind.BROWSER_SCREENSHOT, Kind.BROWSER_WAIT, Kind.BROWSER_SCROLL,
                             Kind.BROWSER_DOUBLE_CLICK, Kind.BROWSER_FIND, Kind.BROWSER_EXTRACT_LIST,
                             Kind.BROWSER_DRIVE_INSPECT_FOLDER, Kind.BROWSER_CDP_MOUSE,
                             Kind.BROWSER_CDP_KEY, Kind.BROWSER_TAB_CREATE, Kind.REMOTE_MCP})


# Heuristic patterns for risk classification. Conservative — false-positives
# (treating safe commands as HIGH) are fine because risk is observability +
# hooks input here, not authoritative enforcement. The real enforcement
# lives in master_ai.is_blocked / _cleanup_safety_issue / _SELF_MOD_DENYLIST.
_HIGH_RISK_RUN_PATTERNS = (
    re.compile(r"\brm\s+-[rRfF]+[a-zA-Z]*\s+/", re.I),       # rm -rf /path
    re.compile(r"\brm\s+-[rRfF]+\b(?!.*\.cache)", re.I),     # rm -rf without cache exception
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bchmod\s+-R\s+777\b", re.I),
    re.compile(r"\bchown\s+-R\s+root\b", re.I),
    re.compile(r">\s*/dev/sd[a-z]", re.I),
    re.compile(r">\s*/dev/nvme", re.I),
    re.compile(r"\bcurl\s.*\|\s*(?:bash|sh)\b", re.I),
    re.compile(r"\bwget\s.*\|\s*(?:bash|sh)\b", re.I),
)

_SAFE_RUN_PREFIXES = (
    "ls", "cat", "file", "head", "tail", "wc", "stat", "which", "type",
    "pwd", "echo", "date", "uptime", "uname", "id", "hostname", "df",
    "du", "free", "ps", "top", "true", "test ", "[ ",
)

_DIRECTIVE_LINE_RE = re.compile(
    r"^\s*(RUN|RUNTERM|READ|CREATE|EDIT|REMEMBER|PLAN|DONE|THINK|RUN_SKILL|SEND_EMAIL|BROWSER_CLICK|BROWSER_FILL|BROWSER_FILL_FORM|BROWSER_UPLOAD_FILE|BROWSER_SUBMIT|BROWSER_READ_PAGE_FULL|BROWSER_READ_PAGE|BROWSER_OBSERVE|BROWSER_READ|BROWSER_NAV|BROWSER_CLOSE_TAB|BROWSER_SCREENSHOT|BROWSER_WAIT|BROWSER_SCROLL|BROWSER_DOUBLE_CLICK|BROWSER_FIND|BROWSER_EXTRACT_LIST|BROWSER_DRIVE_INSPECT_FOLDER|BROWSER_CDP_MOUSE|BROWSER_CDP_KEY|BROWSER_TAB_CREATE|REMOTE_MCP):\s*(.*?)\s*$",
    re.IGNORECASE,
)

BROWSER_READONLY_KINDS = frozenset({
    Kind.BROWSER_READ,
    Kind.BROWSER_READ_PAGE,
    Kind.BROWSER_READ_PAGE_FULL,
    Kind.BROWSER_OBSERVE,
    Kind.BROWSER_SCREENSHOT,
    Kind.BROWSER_WAIT,
    Kind.BROWSER_SCROLL,
    Kind.BROWSER_FIND,
    Kind.BROWSER_EXTRACT_LIST,
    Kind.BROWSER_DRIVE_INSPECT_FOLDER,
    Kind.REMOTE_MCP,
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TypedAction:
    """Structured envelope around a single parsed directive.

    Lifecycle:
        PARSED → (PENDING_APPROVAL → APPROVED) → EXECUTING → COMPLETED
                                                          ↘ FAILED
        Any state can transition to BLOCKED (safeguard refused) or
        SKIPPED (mode-aware skip, e.g. plan-mode RUN: queue).
    """

    kind: str
    target: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cwd: Optional[str] = None
    risk: str = Risk.NORMAL
    requires_confirm: bool = True
    timeout_s: int = 60
    created_by_model: str = ""
    source_text: str = ""
    parsed_at: str = field(default_factory=_now_iso)
    status: str = Status.PARSED
    create_content: Optional[str] = None
    edit_old: Optional[str] = None
    edit_new: Optional[str] = None
    read_range: Optional[tuple] = None  # (start_line, end_line) inclusive
    extras: dict = field(default_factory=dict)

    def __post_init__(self):
        # Normalize kind case so callers can pass "run" or "RUN".
        if isinstance(self.kind, str):
            self.kind = self.kind.upper()
        if self.kind not in DIRECTIVE_KINDS:
            raise ValueError(f"unknown kind {self.kind!r}; expected one of {sorted(DIRECTIVE_KINDS)}")
        if not self.risk:
            self.risk = Risk.NORMAL

    def to_dict(self) -> dict:
        d = asdict(self)
        if isinstance(self.read_range, tuple):
            d["read_range"] = list(self.read_range)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TypedAction":
        if not isinstance(d, dict):
            raise TypeError(f"from_dict expects dict, got {type(d).__name__}")
        if "kind" not in d or "target" not in d:
            raise ValueError("TypedAction requires 'kind' and 'target'")
        d2 = dict(d)
        rr = d2.get("read_range")
        if isinstance(rr, list) and len(rr) == 2:
            d2["read_range"] = tuple(rr)
        known = {f for f in cls.__dataclass_fields__}
        extras_in = {k: v for k, v in d2.items() if k not in known}
        kwargs = {k: v for k, v in d2.items() if k in known}
        if extras_in:
            base_extras = kwargs.get("extras") or {}
            base_extras.update(extras_in)
            kwargs["extras"] = base_extras
        return cls(**kwargs)


def classify_risk(action: TypedAction) -> str:
    """Set and return action.risk based on kind + target heuristics.

    Conservative for RUN/RUNTERM: anything matching a destructive pattern
    is HIGH; the rest defaults to NORMAL (or SAFE for read-only commands).
    READ is always SAFE. CREATE/EDIT default to NORMAL — destination-path
    risk (self-modification of master_ai.py etc.) is enforced separately
    by master_ai._SELF_MOD_DENYLIST and is not duplicated here.
    """
    if action.kind == Kind.READ:
        action.risk = Risk.SAFE
        return action.risk
    if action.kind in (Kind.THINK, Kind.DONE, Kind.PLAN):
        action.risk = Risk.SAFE
        return action.risk
    if action.kind in (Kind.RUN_SKILL, Kind.SEND_EMAIL):
        action.risk = Risk.NORMAL
        return action.risk
    if action.kind in (Kind.RUN, Kind.RUNTERM):
        t = (action.target or "").strip()
        low = t.lower()
        for pat in _HIGH_RISK_RUN_PATTERNS:
            if pat.search(t):
                action.risk = Risk.HIGH
                return action.risk
        if low.startswith("sudo "):
            action.risk = Risk.HIGH
            return action.risk
        first_token = low.split(None, 1)[0] if low else ""
        if first_token in {p.strip().rstrip() for p in _SAFE_RUN_PREFIXES if not p.endswith(" ")} or any(
            low.startswith(p) for p in _SAFE_RUN_PREFIXES
        ):
            if "&&" not in t and ";" not in t and "|" not in t:
                action.risk = Risk.SAFE
                return action.risk
        action.risk = Risk.NORMAL
        return action.risk
    if action.kind in (Kind.CREATE, Kind.EDIT):
        action.risk = Risk.NORMAL
        return action.risk
    if action.kind in BROWSER_READONLY_KINDS:
        action.risk = Risk.SAFE
        return action.risk
    action.risk = Risk.NORMAL
    return action.risk


def parse_directive(line: str, *, model: str = "", source_text: str = "",
                    cwd: Optional[str] = None) -> Optional[TypedAction]:
    """Single-line parser. Returns a TypedAction if `line` matches one of the
    directive keywords on its own line, else None.

    Intentionally simpler than master_ai.process_reply's full parser: this
    is the helper subagents/hooks/audit consumers use, where the input is
    already isolated to one directive. Multi-line CREATE/EDIT bodies and
    backtick-parity edge cases stay in master_ai's parser; callers there
    can construct a TypedAction directly via the dataclass.
    """
    if not isinstance(line, str):
        return None
    m = _DIRECTIVE_LINE_RE.match(line)
    if not m:
        return None
    kind = m.group(1).upper()
    target = m.group(2).strip()
    if kind == "BROWSER_SCREENSHOT" and not target:
        target = "viewport"
    if not target:
        return None
    action = TypedAction(
        kind=kind,
        target=target,
        cwd=cwd,
        created_by_model=model or "",
        source_text=source_text or line,
        requires_confirm=(kind in (
            Kind.RUN, Kind.RUNTERM, Kind.CREATE, Kind.EDIT, Kind.RUN_SKILL, Kind.SEND_EMAIL,
            Kind.BROWSER_CLICK, Kind.BROWSER_FILL, Kind.BROWSER_FILL_FORM, Kind.BROWSER_UPLOAD_FILE,
            Kind.BROWSER_SUBMIT, Kind.BROWSER_READ,
            Kind.BROWSER_READ_PAGE, Kind.BROWSER_OBSERVE, Kind.BROWSER_NAV,
            Kind.BROWSER_READ_PAGE_FULL, Kind.BROWSER_CLOSE_TAB, Kind.BROWSER_SCREENSHOT,
            Kind.BROWSER_WAIT, Kind.BROWSER_SCROLL,
            Kind.BROWSER_DOUBLE_CLICK, Kind.BROWSER_FIND, Kind.BROWSER_EXTRACT_LIST,
            Kind.BROWSER_DRIVE_INSPECT_FOLDER, Kind.BROWSER_CDP_MOUSE,
            Kind.BROWSER_CDP_KEY, Kind.BROWSER_TAB_CREATE, Kind.REMOTE_MCP,
        )),
    )
    classify_risk(action)
    return action


def parse_reply(text: str, *, model: str = "",
                cwd: Optional[str] = None) -> list:
    """Parse a multi-line reply for directive lines. Multi-line CREATE/EDIT
    bodies are NOT reassembled here — use master_ai.process_reply for that.
    This helper is for single-line scans (audit, observability previews).
    """
    out: list = []
    if not isinstance(text, str):
        return out
    for raw in text.splitlines():
        action = parse_directive(raw, model=model, source_text=raw, cwd=cwd)
        if action is not None:
            out.append(action)
    return out


def _is_noop_payload(s: str) -> bool:
    """True for empty/placeholder payloads (bare ':', 'true', pure
    punctuation) a model sometimes emits for a directive with nothing real
    to say. A standalone equivalent of master_ai._is_noop_cmd — this module
    intentionally has no master_ai import (see module docstring)."""
    s = (s or "").strip()
    if not s:
        return True
    if s in (":", "true", "True"):
        return True
    if not re.search(r"[A-Za-z0-9]", s):
        return True
    return False


def _strip_wrap(s: str) -> str:
    """Strip one matching pair of wrapping backticks/quotes, mirroring
    master_ai._strip_command_wrap (master_ai.py:9156-9162)."""
    s = (s or "").strip()
    for lead, trail in (("`", "`"), ("'", "'"), ('"', '"'), ("“", "”"), ("‘", "’")):
        if len(s) >= 2 and s.startswith(lead) and s.endswith(trail):
            s = s[1:-1].strip()
            break
    return s


# Kinds master_ai.process_reply() extracts as single-line RUN/READ-shaped
# directives, matched by word-boundary search + backtick-parity suppression
# (master_ai.py:9180-9206, 9210-9226). CREATE/EDIT are handled separately
# below — the legacy body-block builder matches those by line-start anchor
# instead, with no backtick-parity check (master_ai.py:9260-9298).
_SINGLE_LINE_KINDS = (
    Kind.RUN, Kind.RUNTERM, Kind.READ, Kind.REMEMBER,
    Kind.PLAN, Kind.DONE, Kind.THINK, Kind.RUN_SKILL, Kind.SEND_EMAIL,
)


def _extract_single_line_payload(line: str, name: str) -> str:
    parts = re.split(rf"\b{name}:", line, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return ""
    payload = _strip_wrap(parts[1])
    return "" if _is_noop_payload(payload) else payload


def parse_reply_with_bodies(text: str, *, model: str = "", cwd: Optional[str] = None) -> list:
    """Full reply parser with CREATE/EDIT body-block content capture.

    parse_reply() only does single-line scanning with no backtick-parity or
    body-block awareness — fine for its audit/observability callers, but not
    enough to compare against master_ai.process_reply()'s actual extraction
    for parity testing. This function mirrors that extraction more closely:

      - RUN/RUNTERM/READ/REMEMBER/PLAN/DONE/THINK/RUN_SKILL/SEND_EMAIL are
        matched by word-boundary search with backtick-parity suppression
        (mirrors master_ai._real_directive, master_ai.py:9180-9184) — a
        directive keyword mentioned in prose inside backticks does not fire.
      - REMEMBER additionally excludes lines inside a <<<CONTENT/<<<FIND/
        <<<REPLACE body block (mirrors master_ai.py:9235-9247); the other
        single-line kinds share master_ai's own "blindspot" here and are
        NOT body-excluded — that matches legacy behavior exactly, not an
        oversight in this port.
      - CREATE/EDIT are matched by line-start anchor (mirrors master_ai's
        `^\\s*CREATE:`/`^\\s*EDIT:` body-block builder, master_ai.py:9260-
        9298), with create_content/edit_old/edit_new reassembled from
        <<<CONTENT/<<<FIND/<<<REPLACE blocks, plus the fenced-code salvage
        fallback for a bare CREATE: line followed by a markdown fence
        (master_ai.py:9300-9370), including the HTML CSS/JS fence-merging
        special case.

    Still standalone — no master_ai import (see module docstring).
    """
    out: list = []
    if not isinstance(text, str):
        return out
    lines = text.splitlines()

    # REMEMBER-in-body exclusion set (master_ai.py:9235-9247).
    in_body, eligible = False, []
    for ln in lines:
        stripped_up = ln.strip().upper()
        if stripped_up in ("<<<CONTENT", "<<<FIND", "<<<REPLACE"):
            in_body = True
            continue
        if stripped_up in (">>>CONTENT", ">>>FIND", ">>>REPLACE"):
            in_body = False
            continue
        if not in_body:
            eligible.append(ln)

    for kind in _SINGLE_LINE_KINDS:
        candidate_lines = eligible if kind == Kind.REMEMBER else lines
        for ln in candidate_lines:
            fired = False
            for m in re.finditer(rf"\b{kind}:", ln, re.IGNORECASE):
                if ln[:m.start()].count("`") % 2 == 0:
                    fired = True
                    break
            if not fired:
                continue
            payload = _extract_single_line_payload(ln, kind)
            if not payload:
                continue
            action = TypedAction(
                kind=kind, target=payload, cwd=cwd,
                created_by_model=model or "", source_text=ln,
                requires_confirm=(kind in (Kind.RUN, Kind.RUNTERM, Kind.RUN_SKILL, Kind.SEND_EMAIL)),
            )
            classify_risk(action)
            out.append(action)

    # CREATE/EDIT body-block state machine (master_ai.py:9260-9298).
    in_block, cur_path, cur_content = False, None, []
    cur_find, cur_replace, in_find, in_replace = None, None, False, False
    created_targets: list = []
    edited_targets: list = []
    for line in lines:
        if re.match(r"^\s*CREATE:", line, re.IGNORECASE):
            cur_path = os.path.expanduser(
                re.split(r"CREATE:", line, maxsplit=1, flags=re.IGNORECASE)[1].strip())
            cur_content = []
            in_block = False
        elif line.strip().upper() == "<<<CONTENT" and cur_path:
            in_block = True
        elif line.strip().upper() == ">>>CONTENT" and in_block:
            in_block = False
            created_targets.append((cur_path, "\n".join(cur_content)))
            cur_path = None
        elif in_block:
            cur_content.append(line)
        elif re.match(r"^\s*EDIT:", line, re.IGNORECASE):
            cur_path = os.path.expanduser(
                re.split(r"EDIT:", line, maxsplit=1, flags=re.IGNORECASE)[1].strip())
            cur_find = []; cur_replace = []; in_find = False; in_replace = False
        elif line.strip().upper() == "<<<FIND" and cur_path:
            in_find = True
        elif line.strip().upper() == ">>>FIND" and in_find:
            in_find = False
        elif line.strip().upper() == "<<<REPLACE" and cur_path:
            in_replace = True
        elif line.strip().upper() == ">>>REPLACE" and in_replace:
            in_replace = False
            if cur_find is not None and cur_replace is not None:
                edited_targets.append((cur_path, "\n".join(cur_find), "\n".join(cur_replace)))
            cur_path = None; cur_find = None; cur_replace = None
        elif in_find and cur_find is not None:
            cur_find.append(line)
        elif in_replace and cur_replace is not None:
            cur_replace.append(line)

    # Fenced-code salvage fallback for a bare CREATE: with no proper body
    # (master_ai.py:9300-9370).
    created_paths_seen = {os.path.realpath(p) for p, _ in created_targets}
    for m in re.finditer(r"(?im)^\s*CREATE:\s*(.+?)\s*$", text):
        raw_path = _strip_wrap(m.group(1))
        if not raw_path:
            continue
        exp_path = os.path.expanduser(raw_path)
        real_path = os.path.realpath(exp_path)
        if real_path in created_paths_seen:
            continue
        tail = text[m.end():]
        next_directive = re.search(r"(?im)^\s*(RUN|RUNTERM|READ|CREATE|EDIT|ASK|DONE):", tail)
        create_tail = tail[:next_directive.start()] if next_directive else tail
        reversed_block = re.search(r"(?is)^\s*>>>CONTENT\s*\n(.*?)\n\s*<<<CONTENT\s*", create_tail)
        if reversed_block:
            content = reversed_block.group(1).strip("\n")
            if content:
                created_targets.append((exp_path, content))
                created_paths_seen.add(real_path)
            continue
        fences = list(re.finditer(r"```([A-Za-z0-9_-]+)?\s*\n(.*?)\n```", create_tail, re.DOTALL))
        if fences:
            content = fences[0].group(2).strip("\n")
            if exp_path.lower().endswith((".html", ".htm")):
                css_chunks: list = []
                js_chunks: list = []
                for fm in fences[1:]:
                    lang = (fm.group(1) or "").lower()
                    body = fm.group(2).strip("\n")
                    if lang == "css":
                        css_chunks.append(body)
                    elif lang in ("js", "javascript"):
                        js_chunks.append(body)
                if css_chunks:
                    style_block = "<style>\n" + "\n\n".join(css_chunks) + "\n</style>"
                    content = re.sub(
                        r"\s*<link[^>]+href=[\"']styles\.css[\"'][^>]*>\s*",
                        "\n    " + style_block + "\n",
                        content,
                        flags=re.IGNORECASE,
                    )
                    if style_block not in content:
                        content = content.replace("</head>", f"    {style_block}\n</head>", 1)
                if js_chunks:
                    script_block = "<script>\n" + "\n\n".join(js_chunks) + "\n</script>"
                    content = re.sub(
                        r"\s*<script[^>]+src=[\"']scripts\.js[\"'][^>]*>\s*</script>\s*",
                        "\n    " + script_block + "\n",
                        content,
                        flags=re.IGNORECASE,
                    )
                    if script_block not in content:
                        content = content.replace("</body>", f"    {script_block}\n</body>", 1)
            if content:
                created_targets.append((exp_path, content))
                created_paths_seen.add(real_path)

    for path, content in created_targets:
        action = TypedAction(
            kind=Kind.CREATE, target=path, cwd=cwd,
            created_by_model=model or "", source_text=f"CREATE: {path}",
            create_content=content, requires_confirm=True,
        )
        classify_risk(action)
        out.append(action)

    for path, find_text, replace_text in edited_targets:
        action = TypedAction(
            kind=Kind.EDIT, target=path, cwd=cwd,
            created_by_model=model or "", source_text=f"EDIT: {path}",
            edit_old=find_text, edit_new=replace_text, requires_confirm=True,
        )
        classify_risk(action)
        out.append(action)

    return out


# Mapping from legacy audit kinds (master_ai._audit calls) to the typed
# outcome field. Keys are matched by exact match OR longest-prefix match,
# whichever is more specific.
_AUDIT_OUTCOME_MAP = {
    "RUN":                    ("RUN", Status.COMPLETED),
    "RUN-AUTO":               ("RUN", Status.COMPLETED),
    "RUN-ALWAYS":             ("RUN", Status.COMPLETED),
    "RUN-EMPTY":              ("RUN", Status.BLOCKED),
    "RUN-BLOCK":              ("RUN", Status.BLOCKED),
    "RUN-BLOCK-CLEANUP":      ("RUN", Status.BLOCKED),
    "RUN-BLOCK-MISSING":      ("RUN", Status.BLOCKED),
    "RUN-BLOCK-CONTINUATION": ("RUN", Status.BLOCKED),
    "RUN-SUDO-HANDOFF":       ("RUN", Status.PENDING_APPROVAL),
    "RUN-SUDO-RESUME":        ("RUN", Status.COMPLETED),
    "RUN-SUDO-SKIP":          ("RUN", Status.SKIPPED),
    "RUNTERM":                ("RUNTERM", Status.COMPLETED),
    "RUNTERM-EMPTY":          ("RUNTERM", Status.BLOCKED),
    "RUNTERM-BLOCK":          ("RUNTERM", Status.BLOCKED),
    "RUNTERM-REDIRECT":       ("RUNTERM", Status.COMPLETED),
    "RUNTERM-REDIRECT-DESKTOP": ("RUNTERM", Status.COMPLETED),
    "RUNTERM-BLOCK-CONTINUATION": ("RUNTERM", Status.BLOCKED),
    "RUNTERM-BLOCK-MISSING":  ("RUNTERM", Status.BLOCKED),
    "RUNTERM-EMPTY-PAYLOAD":  ("RUNTERM", Status.BLOCKED),
    "READ":                   ("READ", Status.COMPLETED),
    "READ-BLOCK":             ("READ", Status.BLOCKED),
    "CREATE":                 ("CREATE", Status.COMPLETED),
    "CREATE-BLOCK":           ("CREATE", Status.BLOCKED),
    "EDIT":                   ("EDIT", Status.COMPLETED),
    "EDIT-BLOCK":             ("EDIT", Status.BLOCKED),
    # REMEMBER (self-write to memory) — added 2026-05-11.
    "REMEMBER":               ("REMEMBER", Status.COMPLETED),
    "REMEMBER-EMPTY":         ("REMEMBER", Status.SKIPPED),
    "REMEMBER-DUP":           ("REMEMBER", Status.SKIPPED),
    "DESKTOP-OPEN":           ("RUN", Status.COMPLETED),
    "DESKTOP-REDIRECT":       ("RUN", Status.COMPLETED),
    "POLICY-CMD-BLOCK":       ("RUN", Status.BLOCKED),
    "POLICY-RUNTERM-BLOCK":   ("RUNTERM", Status.BLOCKED),
    "POLICY-REQUEST-BLOCK":   ("REQUEST", Status.BLOCKED),
    "DENY-NO-TTY":            ("RUN", Status.BLOCKED),
    "DENY-EOF":               ("RUN", Status.BLOCKED),
}


def audit_outcome_from_kind(audit_kind: str) -> tuple:
    """Return (directive_kind, status) for a legacy audit kind string.

    Falls back to a best-guess prefix match for kinds not in the table.
    Returns (None, None) if no directive kind can be inferred (e.g. a
    non-directive audit line for menu navigation).
    """
    if not isinstance(audit_kind, str) or not audit_kind:
        return (None, None)
    if audit_kind in _AUDIT_OUTCOME_MAP:
        return _AUDIT_OUTCOME_MAP[audit_kind]
    # Longest-prefix fallback
    for prefix in ("RUNTERM", "RUN", "READ", "CREATE", "EDIT"):
        if audit_kind.upper().startswith(prefix):
            inferred_status = (
                Status.BLOCKED if "BLOCK" in audit_kind.upper()
                else Status.SKIPPED if "SKIP" in audit_kind.upper()
                else Status.PENDING_APPROVAL if "HANDOFF" in audit_kind.upper()
                else Status.COMPLETED
            )
            return (prefix, inferred_status)
    return (None, None)


def make_audit_record(*, kind: str, detail: str,
                      profile: str = "default",
                      mode: str = "",
                      cwd: str = "",
                      model: str = "",
                      action_id: Optional[str] = None) -> Optional[dict]:
    """Build a typed jsonl audit record from a legacy _audit() call.

    Returns None if the audit kind is NOT a directive kind (so callers can
    skip non-directive audit lines). The returned dict is JSON-serializable
    and stable across versions — adding fields here is a SemVer minor
    change; renaming or removing is major.
    """
    directive_kind, status = audit_outcome_from_kind(kind)
    if directive_kind is None or directive_kind == "REQUEST":
        return None
    detail = detail or ""
    try:
        action = TypedAction(
            kind=directive_kind,
            target=detail[:1000],
            cwd=cwd or None,
            created_by_model=model or "",
            source_text=detail[:200],
            status=status,
        )
        classify_risk(action)
    except ValueError:
        return None
    return {
        "id": action.id,
        "ts": _now_iso(),
        "profile": profile or "default",
        "mode": mode or "",
        "cwd": cwd or "",
        "audit_kind": kind,
        "kind": action.kind,
        "target": action.target,
        "risk": action.risk,
        "status": action.status,
        "created_by_model": action.created_by_model,
    }


def serialize(action: TypedAction) -> str:
    """JSON-encode a TypedAction for jsonl logs."""
    return json.dumps(action.to_dict(), default=str, sort_keys=True)


# ── Result envelope ──────────────────────────────────────────────────────
# Status taxonomy that flows BACK to the model via [PREVIOUS ROUND RESULTS].
# Distinct from `Status` above (internal lifecycle: PARSED → APPROVED → ...).
# ResultStatus is the dispatcher's verdict, what the model sees next round.
# Witnessed 2026-05-14 on a Drive selector loop: the model retried
# `div[data-tooltip*="..."]` twice after both `result: failure` rounds
# because the failure shape had no error_code and no observed_tab_url.
# The envelope fields below are the truth the next round needs.


class ResultStatus:
    PLANNED = "planned"                          # Plan mode preview; not executed
    WAITING_FOR_APPROVAL = "waiting_for_approval"  # Review (or Auto+sensitive)
    RUNNING = "running"                          # Auto, safe, in flight
    SUCCESS = "success"                          # Dispatcher executed; succeeded
    FAILURE = "failure"                          # Dispatcher executed; failed
    BLOCKED = "blocked"                          # Dispatcher refused outright


RESULT_STATUSES = frozenset({
    ResultStatus.PLANNED, ResultStatus.WAITING_FOR_APPROVAL, ResultStatus.RUNNING,
    ResultStatus.SUCCESS, ResultStatus.FAILURE, ResultStatus.BLOCKED,
})


@dataclass
class ActionResult:
    """Truthful per-action result fed back to the model.

    The model proposes; the dispatcher decides. Every directive emitted
    gets one of these envelopes in the next round's [PREVIOUS ROUND
    RESULTS] block — generalizes the [TOOL BLOCKED] retry pattern from
    commit 45f6072 (blocked-only feedback) to every-action feedback.
    """
    action_id: str
    kind: str
    target: str
    status: str                                  # one of RESULT_STATUSES
    executed: bool                               # did dispatcher actually run it
    mode_at_emission: str = "plan"               # plan | review | auto
    error_code: Optional[str] = None             # permission_required | target_not_found | nav_blocked | timeout | conflict | …
    error_message: Optional[str] = None
    observed_tab_url: Optional[str] = None       # ground truth post-action; required for every BROWSER_* result
    observed_text: Optional[str] = None          # short snippet for READ
    gated_by: Optional[str] = None               # if downgraded or refused
    ts: str = field(default_factory=_now_iso)
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def make_envelope_from_side_panel_payload(raw: dict) -> ActionResult:
    """Map side_panel.js's existing /extension/action_result body into an
    ActionResult. The wire format is stable — `raw` has keys action_id,
    verdict, result, final_state, action, gated_by. Don't break that
    shape; translate it.
    """
    if not isinstance(raw, dict):
        raise TypeError("side panel payload must be a dict")
    raw_action = raw.get("action") or {}
    kind = str(raw_action.get("kind") or raw.get("kind") or "").upper()
    target = str(raw_action.get("target") or raw.get("target") or "")
    action_id = str(raw.get("action_id") or raw_action.get("id") or "")
    final = raw.get("final_state") if isinstance(raw.get("final_state"), dict) else {}
    result = str(raw.get("result") or "").lower()
    verdict = str(raw.get("verdict") or "").lower()

    # Map (verdict, result) → ResultStatus + executed.
    if verdict == "decline" or verdict == "reject":
        status, executed = ResultStatus.BLOCKED, False
        error_code = "user_declined"
    elif result == "success":
        status, executed = ResultStatus.SUCCESS, True
        error_code = None
    elif result == "failure":
        status, executed = ResultStatus.FAILURE, True
        error_code = _infer_error_code(final)
    elif result == "blocked":
        status, executed = ResultStatus.BLOCKED, False
        error_code = "dispatcher_blocked"
    else:
        status, executed = ResultStatus.WAITING_FOR_APPROVAL, False
        error_code = None

    error_message = final.get("error") if isinstance(final, dict) else None
    if error_message and not isinstance(error_message, str):
        error_message = str(error_message)

    # observed_tab_url: prefer explicit navigated URL on BROWSER_NAV, else
    # the page_context's url (the URL the page settled at after action).
    observed_tab_url = None
    if isinstance(final, dict):
        observed_tab_url = (
            final.get("navigated")
            or ((final.get("page_context") or {}).get("url") if isinstance(final.get("page_context"), dict) else None)
            or final.get("observed_tab_url")
        )

    observed_text = None
    if isinstance(final, dict):
        text = final.get("text")
        if isinstance(text, str):
            observed_text = text[:240]

    gated_by = raw.get("gated_by") if isinstance(raw.get("gated_by"), str) else None
    mode_at_emission = str(raw.get("mode_at_emission") or raw_action.get("mode_at_emission") or "auto").lower()

    return ActionResult(
        action_id=action_id,
        kind=kind,
        target=target[:300],
        status=status,
        executed=executed,
        mode_at_emission=mode_at_emission,
        error_code=error_code,
        error_message=(error_message[:300] if isinstance(error_message, str) else None),
        observed_tab_url=(observed_tab_url[:500] if isinstance(observed_tab_url, str) else None),
        observed_text=observed_text,
        gated_by=gated_by,
        extras={"source_payload_keys": sorted(list(raw.keys()))},
    )


def _infer_error_code(final_state: dict) -> Optional[str]:
    if not isinstance(final_state, dict):
        return None
    err = (final_state.get("error") or "").lower() if isinstance(final_state.get("error"), str) else ""
    if not err:
        return None
    if "target not found" in err or "selector not found" in err:
        return "target_not_found"
    if "permission" in err or "blocked" in err:
        return "permission_required"
    if "timeout" in err or "timed out" in err:
        return "timeout"
    if "navigation" in err or "nav" in err:
        return "nav_blocked"
    if "conflict" in err or "already has" in err:
        return "conflict"
    return "dispatcher_error"


def format_envelope_row(env: ActionResult) -> str:
    """Render one ActionResult as the model-facing text block.

    Schema chosen to fit comfortably inside [PREVIOUS ROUND RESULTS]:
        · BROWSER_NAV https://drive.google.com
          status: failure  executed: false
          error_code: permission_required
          error_message: ...
          observed_tab_url: https://www.indeed.com/...
    """
    if not isinstance(env, ActionResult):
        raise TypeError("format_envelope_row expects ActionResult")
    header = f"  · {env.kind} {env.target}"
    lines = [header,
             f"    status: {env.status}  executed: {'true' if env.executed else 'false'}"]
    if env.error_code:
        lines.append(f"    error_code: {env.error_code}")
    if env.error_message:
        lines.append(f"    error_message: {env.error_message}")
    if env.observed_tab_url:
        lines.append(f"    observed_tab_url: {env.observed_tab_url}")
    if env.observed_text:
        lines.append(f"    observed_text: {env.observed_text}")
    if env.gated_by:
        lines.append(f"    gated_by: {env.gated_by}")
    return "\n".join(lines)
