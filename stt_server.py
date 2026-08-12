#!/usr/bin/env python3
import sys, os, json, tempfile, re, gzip, urllib.request, urllib.error, urllib.parse, threading, time, uuid, importlib.util
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from datetime import datetime

_CLIENT_DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

SCRIPTS    = os.path.expanduser("~/scripts")
_DEFAULT_CHATS_DIR  = os.path.expanduser("~/.master_ai_chats")
os.makedirs(_DEFAULT_CHATS_DIR, exist_ok=True)

def _active_profile():
    """Return active profile name, or '' for default/legacy."""
    try:
        p = os.path.expanduser('~/.master_ai_active_profile')
        if os.path.exists(p):
            name = open(p).read().strip()
            if name and os.path.isdir(os.path.expanduser(f'~/.master_ai_profiles/{name}')):
                return name
    except Exception:
        pass
    return ''

def _chats_dir():
    """Profile-aware chats dir. Falls back to legacy global dir."""
    prof = _active_profile()
    if prof:
        d = os.path.expanduser(f'~/.master_ai_profiles/{prof}/chats')
        os.makedirs(d, exist_ok=True)
        return d
    return _DEFAULT_CHATS_DIR

# Module-level alias for backwards-compat; per-request handlers should call _chats_dir()
CHATS_DIR = _DEFAULT_CHATS_DIR

# Wedge protection (2026-05-14). api_handle wraps the dispatch path in this
# lane lock because _m.handle() monkey-patches module-level globals
# (process_reply, confirm_run, MODE, PINNED_MODEL). A single shared module
# made every request queue behind local Ollama inference, including Groq /
# DeepSeek lanes that never touch Ollama. Keep one imported master_ai module
# per execution lane and serialize only within that lane's module instance.
# Ceiling so a wedged lane is never held indefinitely; callers surface HTTP 503.
_API_HANDLE_LOCK_TIMEOUT_S = 120.0
_API_HANDLE_LANES = ("local", "cloud_fast", "cloud_deep", "cloud", "cloud_vision", "router")
_API_HANDLE_LOCKS = {lane: threading.Lock() for lane in _API_HANDLE_LANES}
_API_MASTER_AI_MODULES = {}
_API_MASTER_AI_MODULES_LOCK = threading.Lock()


class ApiHandleBusy(Exception):
    """Raised when api_handle cannot acquire a per-lane dispatch lock within the
    configured timeout. Caller maps this to HTTP 503 + retry_after so
    clients (Pupil, Chrome extension) fail fast instead of hanging."""


def _api_lane_for_route(route, model=""):
    route = str(route or "").strip().lower()
    model = str(model or "").strip().lower()
    if route == "cloud_fast":
        return "cloud_fast"
    if route == "cloud_deep":
        return "cloud_deep"
    if route == "cloud_vision":
        return "cloud_vision"
    if route == "cloud":
        return "cloud_deep" if model == "deepseek-r1" else "cloud"
    return "local"


def _api_master_module_name(lane):
    safe_lane = re.sub(r"[^a-z0-9_]+", "_", str(lane or "local").lower())
    return f"_master_ai_api_{safe_lane}"


def _load_api_master_ai_module(lane):
    _scripts_on_path()
    lane = str(lane or "local").strip().lower() or "local"
    module_name = _api_master_module_name(lane)
    module_path = os.path.join(SCRIPTS, "master_ai.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load master_ai module for lane {lane}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _get_api_master_ai_module(lane):
    lane = str(lane or "local").strip().lower() or "local"
    with _API_MASTER_AI_MODULES_LOCK:
        module = _API_MASTER_AI_MODULES.get(lane)
        if module is None:
            module = _load_api_master_ai_module(lane)
            _API_MASTER_AI_MODULES[lane] = module
        return module


def _predict_api_handle_lane(history, api_user_text, requested_model=""):
    requested_model = str(requested_model or "").strip().lower()
    if requested_model == "deepseek-r1":
        return "cloud_deep"
    if requested_model in {"groq"}:
        return "cloud_fast"
    if requested_model in {"fireworks", "cerebras"}:
        return "cloud"
    if requested_model in {"gemini"}:
        return "cloud_vision"
    try:
        _router = _get_api_master_ai_module("router")
        decision = _router.orchestrate(list(history or []), api_user_text or "")
        if isinstance(decision, dict):
            return _api_lane_for_route(decision.get("route"), decision.get("model"))
    except Exception:
        pass
    lowered = str(api_user_text or "").lower()
    if "[user prompt]" in lowered:
        lowered = lowered.split("[user prompt]", 1)[-1].lstrip()
    if lowered.startswith("fast:"):
        return "cloud_fast"
    if lowered.startswith("deep:"):
        return "cloud_deep"
    return "local"


_API_HISTORY_LOCK = threading.Lock()
_API_HISTORIES = {}
_API_MAX_HISTORY_MESSAGES = 24

# M9 — Agentic Continuation Loop (scaffold 2026-05-12).
# Each /chat call mints a turn_id. /chat/continue references it via parent_turn_id
# and feeds the extension-reported action_results back into the model. Round
# budget caps runaway loops; user Stop button is the hard interrupt. The model
# emitting an empty actions[] AND no DONE directive also terminates.
_API_TURNS = {}  # {turn_id: {session_key, round_num, round_budget, created_at, parent_turn_id}}
_API_TURNS_LOCK = threading.Lock()
_API_DEFAULT_ROUND_BUDGET = 3
_API_MAX_ROUND_BUDGET = 8  # Hard ceiling; extension can't ask for more.
# Browser-automation turns (chrome_extension source with page_context)
# need a higher default budget than the generic Pupil-chat default.
# Real flows like "search Drive → open folder → wait for SPA render →
# read contents → maybe try a spelling variant" easily eat 6-12 rounds.
# Observed 2026-05-14: Drive résumé-folder lookup ran 15+ rounds at the
# old default-of-3 ceiling, looking exactly like a runaway loop when it
# was really a fundamentally multi-step exploration. Browser turns get
# 8 by default; Pupil chat stays at 3. The max ceiling lifts to 16 so
# legitimate long flows don't false-terminate.
_API_BROWSER_DEFAULT_ROUND_BUDGET = 3
_API_BROWSER_MAX_ROUND_BUDGET = 6   # hard cap; tighter to stop runaway loops
_HEALTH_CACHE_LOCK = threading.Lock()
_HEALTH_CACHE_TTL_S = 3.0
_HEALTH_CACHE = {"ts": 0.0, "payload": None}
_ACTION_LINE_RE = re.compile(
    r"^\s*(RUNTERM|RUN|READ|CREATE|EDIT|REMEMBER|BROWSER_CLICK|BROWSER_FILL|BROWSER_FILL_FORM|BROWSER_UPLOAD_FILE|BROWSER_SUBMIT|BROWSER_READ_PAGE_FULL|BROWSER_READ_PAGE|BROWSER_OBSERVE|BROWSER_READ|BROWSER_NAV|BROWSER_CLOSE_TAB|BROWSER_SCREENSHOT|BROWSER_WAIT|BROWSER_SCROLL|BROWSER_DOUBLE_CLICK|BROWSER_FIND|BROWSER_EXTRACT_LIST|BROWSER_DRIVE_INSPECT_FOLDER|BROWSER_CDP_MOUSE|BROWSER_CDP_KEY|BROWSER_TAB_CREATE|BROWSER_JS|BROWSER_CONSOLE|BROWSER_NETWORK|BROWSER_RESIZE_WINDOW|REMOTE_MCP):\s*(.*?)\s*$",
    re.IGNORECASE,
)

# Domain classifier (Phase 1.1). The list lives at
# ~/.master_ai_domain_classes.json and is refreshed by the maintenance window
# (~/scripts/refresh_domain_classes.sh). Categories:
#   0 = ok (default for any domain not in the file)
#   1 = known malicious/phishing — HARD BLOCK at the extension
#   2 = sensitive auth surface (banking/health/gov auth) — HARD BLOCK
#   3 = high-friction (adult/gambling/crypto-exchange) — force confirm every action
# Domain entries match as suffix labels: 'foo.com' hits 'foo.com',
# 'www.foo.com', 'login.foo.com', etc., but NOT 'badfoo.com'.
_DOMAIN_CLASSES_PATH = os.path.expanduser('~/.master_ai_domain_classes.json')
_DOMAIN_CLASSES_LOCK = threading.Lock()
_DOMAIN_CLASSES_TTL_S = 60.0
_DOMAIN_CLASSES_CACHE = {"ts": 0.0, "data": None, "mtime": 0.0}
_DOMAIN_RESULT_TTL_S = 300


def _load_domain_classes():
    """Return the parsed domain-classes dict. Reloads when the file mtime changes
    or the in-memory copy is older than _DOMAIN_CLASSES_TTL_S. Returns an empty
    dict shape if the file is missing or unreadable (fail-open at the loader;
    callers default to category 0, which is the safe boundary)."""
    now = time.time()
    try:
        st = os.stat(_DOMAIN_CLASSES_PATH)
        mtime = st.st_mtime
    except OSError:
        mtime = 0.0
    with _DOMAIN_CLASSES_LOCK:
        cached = _DOMAIN_CLASSES_CACHE.get("data")
        cached_mtime = float(_DOMAIN_CLASSES_CACHE.get("mtime") or 0.0)
        cached_ts = float(_DOMAIN_CLASSES_CACHE.get("ts") or 0.0)
        if cached is not None and mtime == cached_mtime and (now - cached_ts) < _DOMAIN_CLASSES_TTL_S:
            return cached
    parsed = {"category_1": {}, "category_2": {}, "category_3": {}, "_meta": {}}
    try:
        with open(_DOMAIN_CLASSES_PATH, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for key in ("category_1", "category_2", "category_3", "_meta"):
                val = raw.get(key)
                if isinstance(val, dict):
                    parsed[key] = val
    except (OSError, ValueError):
        pass
    with _DOMAIN_CLASSES_LOCK:
        _DOMAIN_CLASSES_CACHE["data"] = parsed
        _DOMAIN_CLASSES_CACHE["mtime"] = mtime
        _DOMAIN_CLASSES_CACHE["ts"] = now
    return parsed


def _extract_host(domain_or_url):
    """Pull the host label out of either a bare domain string or a URL. Lowercase,
    strip port, strip userinfo. Returns '' for input we can't parse."""
    raw = str(domain_or_url or '').strip()
    if not raw:
        return ''
    if '://' in raw:
        try:
            parsed = urllib.parse.urlparse(raw)
            host = parsed.hostname or ''
        except Exception:
            host = ''
    else:
        host = raw.split('/', 1)[0]
        if '@' in host:
            host = host.rsplit('@', 1)[-1]
        if ':' in host:
            host = host.split(':', 1)[0]
    return host.lower().strip('.')


def _domain_matches(host, entry):
    """Suffix label match. 'foo.com' matches 'foo.com' and 'sub.foo.com' but
    NOT 'badfoo.com'. Empty entry never matches."""
    host = (host or '').lower().strip('.')
    entry = (entry or '').lower().strip('.')
    if not host or not entry:
        return False
    if host == entry:
        return True
    return host.endswith('.' + entry)


def _classify_domain(domain_or_url, classes=None):
    """Classify a domain or URL against the on-disk class list. Returns:
        {category: int, reason: str, matched: str, host: str, ttl_s: int, source: str}
    category 0 is the default when no entry matches. Higher categories win when
    a host matches multiple buckets (1 > 2 > 3) — we apply the strictest."""
    host = _extract_host(domain_or_url)
    if classes is None:
        classes = _load_domain_classes()
    if not host:
        return {
            "category": 0,
            "reason": "no host parsed",
            "matched": "",
            "host": "",
            "ttl_s": _DOMAIN_RESULT_TTL_S,
            "source": "default",
        }
    for cat_num in (1, 2, 3):
        bucket = classes.get(f"category_{cat_num}") or {}
        for entry, reason in bucket.items():
            if _domain_matches(host, entry):
                return {
                    "category": cat_num,
                    "reason": str(reason or "")[:500],
                    "matched": entry,
                    "host": host,
                    "ttl_s": _DOMAIN_RESULT_TTL_S,
                    "source": "list",
                }
    return {
        "category": 0,
        "reason": "ok",
        "matched": "",
        "host": host,
        "ttl_s": _DOMAIN_RESULT_TTL_S,
        "source": "default",
    }


def _health_payload():
    now = time.time()
    with _HEALTH_CACHE_LOCK:
        cached = _HEALTH_CACHE.get("payload")
        if cached and now - float(_HEALTH_CACHE.get("ts") or 0.0) < _HEALTH_CACHE_TTL_S:
            out = dict(cached)
            out["cached"] = True
            return out

    ollama_state = 'down'
    model_name = 'master-ai'
    probe_ms = 0
    t0 = time.time()
    try:
        req = urllib.request.Request('http://127.0.0.1:11434/api/tags')
        with urllib.request.urlopen(req, timeout=2) as resp:
            json.loads(resp.read().decode())
        ollama_state = 'active'
    except Exception:
        ollama_state = 'down'
    finally:
        probe_ms = int((time.time() - t0) * 1000)

    payload = {
        'ok': True,
        'ollama': ollama_state,
        'model': model_name,
        'ts': datetime.now().astimezone().isoformat(timespec='seconds'),
        'probe_ms': probe_ms,
        'cached': False,
    }
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE["ts"] = time.time()
        _HEALTH_CACHE["payload"] = dict(payload)
    return payload


def _scripts_on_path():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    return here


def _vision_locate_coords(b64_png, description, *, max_dim=1024):
    """Ask llava where a described UI element is on a screenshot.

    Returns (x, y) in TRUE (pre-downscale) pixel coordinates if llava
    confidently locates the target, or None on 'NONE', malformed output,
    or any error. Caller emits BROWSER_CDP_MOUSE: x,y with these coords.

    The PNG is downscaled to max_dim on its longest side before being
    handed to llava because CPU-llava chokes on full-resolution browser
    captures. Coordinates from the downscaled image are scaled back up
    using the original dimensions before return.
    """
    if not b64_png or not isinstance(description, str) or not description.strip():
        return None
    tmp_path = None
    scale = 1.0
    try:
        import base64 as _b64
        import io as _io
        import tempfile as _tf
        from PIL import Image as _Image
        raw = _b64.b64decode(b64_png)
        img = _Image.open(_io.BytesIO(raw))
        true_w, true_h = img.size
        if true_w <= 0 or true_h <= 0:
            return None
        longest = max(true_w, true_h)
        if longest > max_dim:
            scale = max_dim / longest
            new_size = (max(1, int(true_w * scale)), max(1, int(true_h * scale)))
            img = img.resize(new_size, _Image.LANCZOS)
        ds_w, ds_h = img.size
        with _tf.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img.save(tmp, format="PNG")
            tmp_path = tmp.name
    except Exception:
        return None

    try:
        _scripts_on_path()
        import master_ai as _m
    except Exception:
        try:
            if tmp_path:
                os.unlink(tmp_path)
        except Exception:
            pass
        return None

    prompt = (
        f"Look at this {ds_w}x{ds_h} screenshot. Output ONLY the pixel "
        f"coordinates of the center of the {description.strip()} as 'x,y' "
        "(two integers, comma-separated, 0,0 is top-left). If you cannot "
        "see it on the page, output exactly 'NONE'. No other text, no "
        "quotes, no explanation."
    )
    reply = ""
    try:
        vision_model = _m.MODELS.get("vision") if hasattr(_m, "MODELS") else "llava"
        reply = _m.ask_local(
            [{"role": "user", "content": prompt}],
            model=vision_model,
            image_path=tmp_path,
        ) or ""
    except Exception:
        reply = ""
    finally:
        try:
            if tmp_path:
                os.unlink(tmp_path)
        except Exception:
            pass

    text = str(reply).strip()
    if not text or text.upper().startswith("NONE"):
        return None
    first_line = text.splitlines()[0]
    m = re.match(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*$", first_line)
    if not m:
        return None
    dx, dy = int(m.group(1)), int(m.group(2))
    if not (0 <= dx <= ds_w and 0 <= dy <= ds_h):
        return None
    if scale < 1.0 and scale > 0:
        return (int(round(dx / scale)), int(round(dy / scale)))
    return (dx, dy)


def _api_session_key(source, session_id):
    session_id = (session_id or "").strip()
    if not session_id:
        return ""
    source = (source or "pupil").strip()[:80] or "pupil"
    return f"{source}:{session_id[:160]}"


def _trim_api_history(history):
    if not isinstance(history, list):
        return []
    return history[-_API_MAX_HISTORY_MESSAGES:]


def _safe_context_text(value, limit=1800):
    if value is None:
        return ""
    text = str(value).replace("\r", " ").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


# ─── RANK 1: page_context prompt-injection defense ───────────────────────────
# Plan: ~/.claude/plans/auto-did-not-actually-stateful-wozniak.md
# Step order is fixed: (1) strip bidi/zero-width, (2) collapse whitespace inside
# candidate uppercase tokens, (3) match case-insensitively against the sentinel
# list, (4) replace inline with [scrubbed directive]. Inline replace keeps page
# understanding intact; whole-field omission would trigger compensatory
# hallucination. Audit log records pattern names + counts only — never the
# scrubbed bytes.

_BIDI_ZWSP_CHARS = (
    "​‌‍‎‏"   # ZWSP, ZWNJ, ZWJ, LRM, RLM
    "‪‫‬‭‮"   # LRE, RLE, PDF, LRO, RLO
    "⁦⁧⁨⁩"         # LRI, RLI, FSI, PDI
    "﻿"                            # BOM
)
_BIDI_ZWSP_RE = re.compile(f"[{_BIDI_ZWSP_CHARS}]")

# Full sentinel set surfaced by Pre-Check #1 (handoff doc 2026-05-13).
_DIRECTIVE_VERBS = (
    # Order: longest first so RUNTERM matches before RUN.
    "RUNTERM", "REMEMBER", "BROWSER",  # BROWSER handled separately below
    "CREATE", "READ", "EDIT", "THINK", "DONE", "RUN", "ASK",
)
# Strip the BROWSER placeholder — it gets its own pattern that allows the
# `_<SUFFIX>` shape (`BROWSER_CLICK:`, `BROWSER_NAV:`, etc.).
_VERB_LIST = tuple(v for v in _DIRECTIVE_VERBS if v != "BROWSER")


def _build_verb_pattern(verb):
    """Standard form: `\\bVERB\\s*:` — case-insensitive.

    Catches `RUN:`, `Run:`, `run:`, `RUN  :`. Does NOT catch `R U N :` —
    that's the spaced-obfuscation form handled by `_build_spaced_verb_pattern`.
    Per-verb literal match avoids the greedy-consumption bug where a long
    English phrase ending in a verb gets matched as a single candidate and
    returned unchanged (swallowing the inner directive verbatim).
    """
    return re.compile(rf'\b{re.escape(verb)}\s*:', flags=re.IGNORECASE)


def _build_spaced_verb_pattern(verb):
    """Spaced-obfuscation form: each letter separated by exactly one whitespace.

    Catches `R U N :` (space between each char). Requires AT LEAST one space
    between each pair of letters (using `[ \\t]` not `[ \\t]?`) so it doesn't
    overlap with the standard pattern — the standard pattern already catches
    `RUN:`. 8-char-window constraint is implicit: a 3-letter verb with single
    spaces = 5 chars; a 7-letter verb = 13 chars (still tight).
    """
    spaced = r'[ \t]'.join(re.escape(c) for c in verb)
    return re.compile(rf'\b{spaced}\s*:', flags=re.IGNORECASE)


_VERB_PATTERNS = [
    (verb + ":", _build_verb_pattern(verb)) for verb in _VERB_LIST
]
# Spaced-obfuscation patterns only apply to verbs of length >= 2 (single-letter
# verbs would have no internal whitespace gap to detect).
_SPACED_VERB_PATTERNS = [
    (verb + ":", _build_spaced_verb_pattern(verb))
    for verb in _VERB_LIST if len(verb) >= 2
]

# BROWSER_<UPPER+UNDERSCORE>: — `BROWSER_CLICK:`, `BROWSER_NAV:`, etc.
# Internal whitespace not handled here (rare obfuscation form).
_BROWSER_PATTERN = re.compile(
    r'\bBROWSER_[A-Z_]+\s*:',
    flags=re.IGNORECASE,
)

_BLOCK_MARKERS = (
    "<<<CONTENT", ">>>CONTENT",
    "<<<FIND", ">>>FIND",
    "<<<REPLACE", ">>>REPLACE",
)
_BLOCK_MARKER_RE = re.compile(
    "|".join(re.escape(m) for m in _BLOCK_MARKERS),
    flags=re.IGNORECASE,
)

# <PLAN READY> matched FIRST so <PLAN> doesn't shadow it.
_PLAN_MARKERS = ("<PLAN READY>", "</PLAN>", "<PLAN>")
_PLAN_MARKER_RE = re.compile(
    "|".join(re.escape(m) for m in _PLAN_MARKERS),
    flags=re.IGNORECASE,
)

_SCRUB_REPLACEMENT = "[scrubbed directive]"


def _sanitize_pass(text):
    """Return (sanitized_text, list_of_pattern_names_fired_in_order).

    Pattern names are stable identifiers used in the audit log — never the
    scrubbed bytes themselves. Caller may dedupe; this function preserves
    every firing in order so cross-field accumulation is honest.
    """
    if not text:
        return text or "", []
    fired = []

    # Step 1 — strip bidi/zero-width controls.
    cleaned = _BIDI_ZWSP_RE.sub("", text)
    if cleaned != text:
        fired.append("bidi_strip")

    # Step 2 — scrub standard verb forms (`RUN:`, `RUNTERM:`, etc.) case-
    # insensitively. Per-verb literal match avoids the greedy-candidate bug
    # where a long English phrase ending in `<verb>:` matched as one
    # candidate and returned unchanged, swallowing the inner directive.
    for verb_name, pattern in _VERB_PATTERNS:
        def _scrub_verb(m, vn=verb_name):
            fired.append(vn)
            return _SCRUB_REPLACEMENT
        cleaned = pattern.sub(_scrub_verb, cleaned)

    # Step 2b — spaced-obfuscation forms (`R U N :`). Separate pass so the
    # standard pattern's behavior is unchanged.
    for verb_name, pattern in _SPACED_VERB_PATTERNS:
        def _scrub_spaced(m, vn=verb_name):
            fired.append(vn)
            return _SCRUB_REPLACEMENT
        cleaned = pattern.sub(_scrub_spaced, cleaned)

    # Step 3 — BROWSER_<SUFFIX>: directives.
    def _scrub_browser(m):
        token = re.sub(r'\s+', '', m.group(0).upper()).rstrip(':')
        fired.append(f"{token}:")
        return _SCRUB_REPLACEMENT
    cleaned = _BROWSER_PATTERN.sub(_scrub_browser, cleaned)

    # Step 4 — block markers (`<<<CONTENT`, `>>>CONTENT`, etc.).
    def _scrub_block(m):
        fired.append(m.group(0).upper())
        return _SCRUB_REPLACEMENT
    cleaned = _BLOCK_MARKER_RE.sub(_scrub_block, cleaned)

    # Step 5 — plan markers (`<PLAN READY>` matched FIRST so `<PLAN>` doesn't
    # shadow it; that ordering lives in the regex alternation order).
    def _scrub_plan(m):
        fired.append(m.group(0).upper())
        return _SCRUB_REPLACEMENT
    cleaned = _PLAN_MARKER_RE.sub(_scrub_plan, cleaned)

    return cleaned, fired


def _sanitize_page_context_field(value, field_name, fired_acc, fields_acc):
    """Per-field sanitize. Mutates fired_acc (list) and fields_acc (set)."""
    if value is None:
        return ""
    cleaned, fired = _sanitize_pass(str(value))
    if fired:
        fired_acc.extend(fired)
        fields_acc.add(field_name)
    return cleaned


def _sanitize_assembled_context_block(text, fired_acc, fields_acc):
    """Post-assembly sanitize. Catches cross-field directive splits where a
    hostile page placed half a directive in one field and half in an adjacent
    field — concatenation only assembles them after _format_page_context joins."""
    cleaned, fired = _sanitize_pass(text)
    if fired:
        fired_acc.extend(fired)
        fields_acc.add("_assembled_block")
    return cleaned


def _write_sanitize_audit(*, request_id, source, scrub_meta):
    """Write one audit row when page_context sanitization fired anything.

    Schema (every field required by plan):
      ts, kind, request_id, source, scrub_count, patterns, fields.
    Audit log NEVER stores raw scrubbed bytes — pattern names + counts only,
    so the log itself cannot become a secondary leak surface for hostile page
    content. Test #7 enforces this.
    """
    if not scrub_meta or scrub_meta.get("count", 0) == 0:
        return
    rec = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": "page_context_sanitize",
        "request_id": (request_id or "")[:160],
        "source": (source or "")[:80],
        "scrub_count": int(scrub_meta.get("count", 0)),
        "patterns": list(scrub_meta.get("patterns", [])),
        "fields": list(scrub_meta.get("fields", [])),
    }
    try:
        from pathlib import Path as _P
        with _P.home().joinpath('.master_ai_audit_typed.jsonl').open('a') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass


PAGE_TREE_BYTE_CAP = 24576  # defense-in-depth re-clip if the extension sends more


def _sanitize_tree_in_place(tree, fired_acc, fields_acc, path="tree"):
    """Recursively sanitize string values inside the semantic page tree.

    Mirrors _sanitize_page_context_field on every string leaf; tags
    fields_acc with `tree.<path>` for stable audit attribution. Closes the
    Shadow DOM / cross-field directive-split gap the legacy flat-field
    sanitizer alone could miss (plan §2).
    """
    if isinstance(tree, dict):
        for k, v in list(tree.items()):
            child_path = f"{path}.{k}"
            if isinstance(v, str):
                cleaned, fired = _sanitize_pass(v)
                if fired:
                    fired_acc.extend(fired)
                    fields_acc.add(child_path)
                tree[k] = cleaned
            elif isinstance(v, (dict, list)):
                _sanitize_tree_in_place(v, fired_acc, fields_acc, child_path)
    elif isinstance(tree, list):
        for i, v in enumerate(tree):
            child_path = f"{path}[{i}]"
            if isinstance(v, str):
                cleaned, fired = _sanitize_pass(v)
                if fired:
                    fired_acc.extend(fired)
                    fields_acc.add(child_path)
                tree[i] = cleaned
            elif isinstance(v, (dict, list)):
                _sanitize_tree_in_place(v, fired_acc, fields_acc, child_path)


def _format_tree_node(node):
    """One indented line for a single semantic-tree node."""
    if not isinstance(node, dict):
        return ""
    role = node.get("role") or "?"
    name = node.get("name") or ""
    ref = node.get("ref") or ""
    parts = [f"  {role} \"{name}\""]
    if ref:
        parts.append(f"ref={ref}")
    state = node.get("state")
    if isinstance(state, dict) and state:
        keys = ",".join(sorted(str(k) for k in state.keys()))
        parts.append(f"state={keys}")
    val = node.get("value")
    if val:
        parts.append(f"value={_safe_context_text(val, limit=160)}")
    sel = node.get("selector")
    if sel:
        parts.append(f"selector={_safe_context_text(sel, limit=240)}")
    return " ".join(parts)


def _render_page_tree(tree):
    """Render a semantic page tree to a model-facing block.

    Stable section order so the model sees a predictable layout:
    landmarks → headings → buttons → links → inputs → file_folder_rows →
    dialogs → lists → iframes → truncation. Empty sections are skipped.
    Refs (`r-N`) are the stable handle within one snapshot — model echoes
    them back via `BROWSER_CLICK ref=r-12` etc.
    """
    if not isinstance(tree, dict):
        return ""
    out = []
    source = tree.get("source") or "ax_tree"
    out.append(f"[BROWSER PAGE TREE source={source}]")
    if tree.get("url"):
        out.append(f"url: {_safe_context_text(tree.get('url'), limit=500)}")
    if tree.get("title"):
        out.append(f"title: {_safe_context_text(tree.get('title'), limit=300)}")
    sections = (
        ("landmarks", "landmarks"),
        ("headings", "headings"),
        ("buttons", "buttons"),
        ("links", "links"),
        ("inputs", "inputs"),
        ("file_folder_rows", "file/folder rows"),
        ("dialogs", "dialogs"),
        ("lists", "lists"),
    )
    for key, label in sections:
        items = tree.get(key)
        if not isinstance(items, list) or not items:
            continue
        out.append(f"{label}:")
        for node in items:
            line = _format_tree_node(node)
            if line:
                out.append(line)
    iframes = tree.get("iframes")
    if isinstance(iframes, list) and iframes:
        out.append("iframes:")
        for f in iframes:
            if not isinstance(f, dict):
                continue
            kind = "cross-origin" if f.get("cross_origin") else "same-origin"
            title = _safe_context_text(f.get("title") or f.get("name") or "", limit=160)
            src = _safe_context_text(f.get("src") or "", limit=240)
            ref = f.get("ref") or ""
            reason = _safe_context_text(f.get("unobserved_reason") or "", limit=200)
            parts = [f"  {kind} frame \"{title}\""]
            if ref:
                parts.append(f"ref={ref}")
            if src:
                parts.append(f"src={src}")
            if reason:
                parts.append(f"unobserved={reason}")
            out.append(" ".join(parts))
    trunc = tree.get("truncation")
    if isinstance(trunc, dict) and trunc.get("reason"):
        dropped = trunc.get("dropped_nodes") or 0
        out.append(f"truncation: {trunc.get('reason')} dropped={dropped}")
    return "\n".join(out)


def _format_page_context(page_context):
    """Format browser page_context dict to model-facing text.

    Returns (formatted_text, scrub_meta) where scrub_meta is:
      {"count": int, "patterns": list[str], "fields": list[str]}.

    Per-field sanitization runs BEFORE the per-field cap so the directive
    pattern can't be split by the cap. Assembled-block sanitization runs AFTER
    field concatenation to catch cross-field directive splits. When a `tree`
    field is present (Claude-Chrome-style AX snapshot — plan §2), the tree is
    sanitized recursively and appended as a [BROWSER PAGE TREE] sub-block.
    """
    scrub_meta = {"count": 0, "patterns": [], "fields": []}
    if not isinstance(page_context, dict):
        return "", scrub_meta

    fired_acc = []
    fields_acc = set()

    fields = []
    for key, limit in (
        ("url", 500),
        ("title", 300),
        ("selection", 1200),
        ("focused_text", 1200),
        ("interactive_elements", 2400),
        ("visible_text", 1800),
    ):
        raw = page_context.get(key)
        if raw is None:
            continue
        sanitized = _sanitize_page_context_field(raw, key, fired_acc, fields_acc)
        val = _safe_context_text(sanitized, limit=limit)
        if val:
            fields.append(f"{key}: {val}")

    tree = page_context.get("tree")
    tree_block = ""
    if isinstance(tree, dict):
        try:
            raw_bytes = len(json.dumps(tree).encode("utf-8"))
        except Exception:
            raw_bytes = 0
        if raw_bytes > PAGE_TREE_BYTE_CAP:
            for k in ("file_folder_rows", "lists", "buttons", "links", "inputs",
                      "landmarks", "headings"):
                v = tree.get(k)
                if isinstance(v, list) and len(v) > 4:
                    tree[k] = v[:max(4, len(v) // 2)]
                if len(json.dumps(tree).encode("utf-8")) <= PAGE_TREE_BYTE_CAP:
                    break
            tree.setdefault("truncation", {})["reason"] = "server_byte_cap"
        _sanitize_tree_in_place(tree, fired_acc, fields_acc)
        tree_block = _render_page_tree(tree)
    else:
        # Codex side-panel fallback (debugger-attach from the side panel itself)
        # produces { semantic_tree: { text, source, ... }, browser_read_source }
        # without a top-level `tree`. Surface its text dump as a page-tree block
        # so the model still gets the AX content when the SW path fails.
        st = page_context.get("semantic_tree")
        if isinstance(st, dict) and isinstance(st.get("text"), str) and st["text"].strip():
            sanitized = _sanitize_page_context_field(
                st["text"], "semantic_tree.text", fired_acc, fields_acc
            )
            sanitized = _safe_context_text(sanitized, limit=9000)
            src = _safe_context_text(st.get("source") or "ax_fallback", limit=80)
            tree_block = f"[BROWSER PAGE TREE source={src}]\n{sanitized}"
            if st.get("truncated"):
                tree_block += "\ntruncation: client_text_cap"

    if not fields and not tree_block:
        if fired_acc:
            # Even with no formatted fields, audit truthfully if scrubbing fired.
            scrub_meta.update(_finalize_scrub_meta(fired_acc, fields_acc))
        return "", scrub_meta

    pieces = []
    if fields:
        pieces.append("[BROWSER PAGE CONTEXT]\n" + "\n".join(fields))
    if tree_block:
        pieces.append(tree_block)
    block = "\n".join(pieces)
    block = _sanitize_assembled_context_block(block, fired_acc, fields_acc)

    if fired_acc:
        scrub_meta.update(_finalize_scrub_meta(fired_acc, fields_acc))
    return block, scrub_meta


def _finalize_scrub_meta(fired_acc, fields_acc):
    """Dedupe pattern names (preserve order) and sort fields for stable audit."""
    seen = set()
    unique_patterns = []
    for p in fired_acc:
        if p not in seen:
            seen.add(p)
            unique_patterns.append(p)
    return {
        "count": len(fired_acc),
        "patterns": unique_patterns,
        "fields": sorted(fields_acc),
    }


def _format_tabs_context(tabs_context):
    """Format Phase 4.3 tabs_context list for the model. Returns "" when no
    tabs are present. Sanitizes every url/title leaf the same way page_context
    leaves are sanitized so a malicious tab title can't slip directives in.

    tabs_context shape (from the Chrome extension):
        [{tab_id, url, title, active, in_session_group, status}, ...]
    """
    if not isinstance(tabs_context, list) or not tabs_context:
        return ""
    fired_acc, fields_acc = [], set()
    lines = ["[OPEN TABS]"]
    for entry in tabs_context[:20]:
        if not isinstance(entry, dict):
            continue
        url = _sanitize_page_context_field(entry.get("url"), "tabs_context.url", fired_acc, fields_acc)
        title = _sanitize_page_context_field(entry.get("title"), "tabs_context.title", fired_acc, fields_acc)
        if not url and not title:
            continue
        flags = []
        if entry.get("active"): flags.append("active")
        if entry.get("in_session_group"): flags.append("in_group")
        status = str(entry.get("status") or "").strip().lower()
        if status and status not in ("complete", ""): flags.append(status)
        flag_text = f" ({', '.join(flags)})" if flags else ""
        tab_id = entry.get("tab_id")
        tab_label = f"tab {tab_id}" if isinstance(tab_id, (int, float)) else "tab ?"
        title_text = _safe_context_text(title or "(no title)", limit=160)
        url_text = _safe_context_text(url or "(no url)", limit=400)
        lines.append(f"- {tab_label}{flag_text}: {title_text}  {url_text}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _quick_mode_teaching():
    """Phase 6 — Quick Mode instructs the model to emit one single-letter
    command per reply, ending with `\\n<<END>>`. The extension parses and
    executes one at a time, then sends a fresh /chat round with a screenshot
    as page_context. This mirrors Anthropic's Quick Mode shape on top of
    Lane B's existing routing (no stop_sequences param needed because the
    teaching keeps replies short on its own)."""
    return (
        "QUICK MODE — emit exactly ONE single-letter command per reply, then "
        "the literal token `<<END>>` on its own line. The extension parses "
        "and executes the command, takes a fresh screenshot, and sends the "
        "next round. Commands (case-insensitive):\n"
        "  C x y               click at viewport pixel x,y\n"
        "  T <text>            type text into the focused element\n"
        "  K <key>             press a single key (Enter / Tab / Escape / Backspace / ArrowDown / etc.)\n"
        "  N <url>             navigate the active tab to URL\n"
        "  J <expr>            evaluate a JS expression in the page\n"
        "  W <ms>              wait N milliseconds for SPA settling\n"
        "  ST <tabId>          switch focus to that tab in the session group\n"
        "Reply shape:\n"
        "  one_command_line\n"
        "  <<END>>\n"
        "Do NOT emit the BROWSER_* directives in Quick Mode. Do NOT emit\n"
        "multiple commands in one reply. When the goal is reached, reply with\n"
        "`DONE: <one-line summary>` followed by `<<END>>`.\n"
    )


def _api_prompt(prompt, *, source="", page_context=None, schedule_id="",
                action_results=None, round_num=1, round_budget=None,
                request_id="", resume_path="", local_file_hints=None,
                tabs_context=None, mode=""):
    context, scrub_meta = _format_page_context(page_context)
    # Audit even if no formatted context survived (e.g., every field was empty
    # after sanitize) — the scrub still happened and Elijah needs the row.
    if scrub_meta.get("count", 0) > 0:
        _write_sanitize_audit(
            request_id=request_id,
            source=source or "pupil",
            scrub_meta=scrub_meta,
        )
    results_block = _format_action_results(action_results)
    if not (source or context or schedule_id or results_block):
        return prompt
    lines = [
        "[API REQUEST]",
        f"source: {_safe_context_text(source or 'pupil', 80)}",
    ]
    # Phase 6 — inject Quick Mode teaching at the top of the prompt so it
    # frames every other instruction the model reads below it.
    if str(mode or "").lower() == "quick":
        lines.append("")
        lines.append(_quick_mode_teaching())
    if resume_path:
        # Phase 2.1: tell the model where the user's résumé file is. Used by
        # the model to emit BROWSER_FILL targets like
        # `BROWSER_FILL: input[type="file"] :: file:///home/user/...`. The
        # extension uses /extension/read_local_file to fetch the bytes for
        # DataTransfer-based upload (full bridge in 2.1b).
        lines.append(f"resume_path: {_safe_context_text(resume_path, 500)}")
    if isinstance(local_file_hints, dict) and local_file_hints.get("candidates"):
        hint_lines = []
        for item in list(local_file_hints.get("candidates") or [])[:5]:
            if not isinstance(item, dict):
                continue
            path = _safe_context_text(str(item.get("path") or ""), 500)
            reason = _safe_context_text(str(item.get("reason") or ""), 160)
            score = _safe_context_text(str(item.get("score") or ""), 40)
            if path:
                hint_lines.append(f" - {path} score={score} reason={reason}")
        if hint_lines:
            ambiguous = "true" if local_file_hints.get("ambiguous") else "false"
            lines.append("local_file_hints:")
            lines.append(f" ambiguous: {ambiguous}")
            lines.extend(hint_lines)
    if schedule_id:
        lines.append(f"schedule_id: {_safe_context_text(schedule_id, 120)}")
    if round_num and round_num > 1:
        budget_str = f"/{round_budget}" if round_budget else ""
        lines.append(f"continuation_round: {round_num}{budget_str}")
    lines.extend([
        "Branch B: you have BOTH lanes — browser (via the extension) AND local terminal + filesystem (server-dispatched). Pick whichever lane is most natural for each step; don't claim anything has been executed until the dispatch path returns results.",
        "Local terminal + filesystem lane — for file ops, downloads, PDF text extraction, opening desktop apps, or anything cleaner in a shell, emit RUN, RUNTERM, READ, CREATE, or EDIT directives. The backend runs them server-side and the stdout/output appears appended to the reply context for your next round. Sudo, dangerous patterns, and the self-mod denylist are still gated.",
        "Browser lane — for in-tab work (click, fill, navigate, observe, scroll, screenshot, submit, press a key), emit BROWSER_CLICK, BROWSER_FILL, BROWSER_READ_PAGE, BROWSER_READ, BROWSER_NAV, BROWSER_SCREENSHOT, BROWSER_WAIT, BROWSER_SCROLL, BROWSER_DOUBLE_CLICK, BROWSER_EXTRACT_LIST, BROWSER_CDP_KEY, or BROWSER_CDP_MOUSE directives. The extension confirms and dispatches them in-tab. BROWSER_CDP_KEY sends real keyboard input through CDP — shapes: `press Enter` (also Tab/Escape/Backspace/ArrowDown/ArrowUp/etc.) or `type <text>`. Use it when a form submit needs a real Enter keystroke (search bars, SPA inputs that ignore form.submit()), when BROWSER_SUBMIT doesn't fire the page's keyup handler, or for keyboard-shortcut interactions. BROWSER_CDP_MOUSE clicks at pixel coordinates 'x,y' — use only when DOM selectors keep failing and a [VISION HINT] line in the repair block has given you coords. The server's sensitivity classifier still gates CDP_MOUSE on sensitive page URLs (apply/checkout/submit/share/delete, auth/payment hosts, Drive share dialogs) — those will require explicit confirmation regardless of mode.",
        "Lane choice — code-first: PREFER the terminal lane when both lanes work. A 5-line bash or python script that finishes the step is faster, deterministic, and auditable. Only drive the browser when there is no terminal path — login-walled UI, form with no exposed API, JS-rendered submit, in-tab visual confirmation. Only use the terminal when there is no browser path — system services, package install, file permissions, anything below the URL bar. Never duplicate work across both — pick one per step. Interleave freely (terminal step → browser step → terminal step) within a multi-step plan when each step genuinely needs the lane chosen.",
        "If the user explicitly asks to use a configured remote MCP server, emit REMOTE_MCP with JSON {server, method:'tools/list'|'tools/call', params}. Remote MCP is permission-gated by the extension.",
        "After any browser navigation/open/search/scroll, use the fresh page_context from continuation before choosing the next click; observe, then act.",
        "If the browser task depends on user-named documents, use local READ/RUN extraction first; browser screenshots are verification/fallback, not the document source.",
        "The HTTP API will return directives as actions[] for the extension to confirm.",
        "Do not say a browser action has been completed until [PREVIOUS ROUND RESULTS] shows the extension completed it.",
        "Do not emit DONE in the same reply as BROWSER_* directives; wait for the extension's results first.",
    ])
    if round_num and round_num > 1:
        lines.append(
            "M9 LOOP: the extension already dispatched your previous round's actions. "
            "Read [PREVIOUS ROUND RESULTS] below, decide if the user's goal is met, "
            "and either propose more BROWSER_* actions or reply with a final answer "
            "(empty actions[] = goal complete)."
        )
    if context:
        if scrub_meta.get("count", 0) > 0:
            lines.append("")
            lines.append(f"[SAFETY: {scrub_meta['count']} page-context tokens scrubbed]")
        lines.extend(["", context])
    # Phase 4.3 — tabs_context (list of currently-open tabs in the session
    # group) lands between page_context and results so the model sees what's
    # available before reading the prior round's outcomes.
    tabs_block = _format_tabs_context(tabs_context)
    if tabs_block:
        lines.extend(["", tabs_block])
    if results_block:
        lines.extend(["", results_block])
    lines.extend(["", "[USER PROMPT]", prompt])
    return "\n".join(lines)


def _write_turn_audit(rec):
    """M9 turn-level audit: one JSON record per terminated turn so behavioral
    analytics can query 'what did the user ask, what did the model propose
    across all rounds, and what was the net outcome' from a single source.
    Sibling to /extension/action_result rows but at turn granularity."""
    try:
        from pathlib import Path as _P
        with _P.home().joinpath('.master_ai_audit_typed.jsonl').open('a') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass  # Audit is observability, not a blocker.


def _build_terminal_turn(*, turn_id, turn_root, round_num, round_budget,
                         parent_turn_id, session_key, reply, route, model,
                         t0, terminal_reason):
    """Synthesize a final-round response without calling the model. Used by
    the M9 short-circuit (duplicate failure detection) and any other forced
    termination path. Always returns done=true."""
    round_remaining = max(0, round_budget - round_num)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with _API_TURNS_LOCK:
        _API_TURNS[turn_id] = {
            "session_key": session_key,
            "round_num": round_num,
            "round_budget": round_budget,
            "turn_root": turn_root,
            "parent_turn_id": parent_turn_id or None,
            "created_at": now,
            "done": True,
        }
        if len(_API_TURNS) > 256:
            oldest = sorted(_API_TURNS.items(), key=lambda kv: kv[1].get("created_at", ""))[:64]
            for k, _ in oldest:
                _API_TURNS.pop(k, None)
    _write_turn_audit({
        "ts": now,
        "source": "api_handle",
        "kind": "turn_terminal",
        "turn_id": turn_id,
        "turn_root": turn_root,
        "round_num": round_num,
        "round_budget": round_budget,
        "terminal_reason": terminal_reason,
        "route": route,
        "model": model,
        "actions_count": 0,
    })
    return {
        "reply": reply,
        "route": route,
        "model": model,
        "latency_ms": int((time.time() - t0) * 1000),
        "actions": [],
        "blocked_actions": [],
        "turn_id": turn_id,
        "turn_root": turn_root,
        "round_num": round_num,
        "round_budget": round_budget,
        "round_remaining": round_remaining,
        "done": True,
        "ts": now,
    }


def _duplicate_failure_reason(action_results):
    """M9 safety: if the same (kind, target) failed twice in the incoming batch,
    return a reason string for short-circuit termination. Returns "" otherwise.
    Prevents the loop from burning rounds when the model retries a bad selector."""
    if not isinstance(action_results, list) or not action_results:
        return ""
    seen_failures = {}
    for ar in action_results:
        if not isinstance(ar, dict):
            continue
        if (ar.get("result") or "").lower() != "failure":
            continue
        act = ar.get("action") or {}
        kind = str(act.get("kind") or ar.get("kind") or "").upper()
        target = str(act.get("target") or ar.get("target") or "")[:300]
        if not kind or not target:
            continue
        key = (kind, target)
        seen_failures[key] = seen_failures.get(key, 0) + 1
        if seen_failures[key] >= 2:
            return f"same target failed twice in one round: {kind} {target!r}"
    return ""


def _format_action_results(action_results):
    """Format extension-reported action outcomes into [PREVIOUS ROUND RESULTS].

    Phase 1 commit 1.4: emit the typed envelope shape (with observed_tab_url
    + error_code) via typed_actions.format_envelope_row. The next round's
    model sees ground truth — if observed_tab_url doesn't match the URL it
    tried to navigate to, the navigation didn't happen. Falls back to the
    legacy compact format when typed_actions isn't available or a row's
    shape is incompatible (e.g., from older side_panel builds).
    """
    if not isinstance(action_results, list) or not action_results:
        return ""

    try:
        _scripts_on_path()
        import typed_actions as _ta
        envelopes = []
        for ar in action_results[:20]:
            if not isinstance(ar, dict):
                continue
            try:
                env = _ta.make_envelope_from_side_panel_payload(ar)
                envelopes.append(_ta.format_envelope_row(env))
            except Exception:
                # Per-row failure: degrade to a single legacy line for that
                # row, keep going on the rest.
                envelopes.append(_format_action_result_legacy(ar))
        if not envelopes:
            return ""
        return "[PREVIOUS ROUND RESULTS]\n" + "\n".join(envelopes)
    except Exception:
        # typed_actions unavailable (shouldn't happen in normal deployment);
        # fall back to legacy formatting for all rows.
        rows = [_format_action_result_legacy(ar) for ar in action_results[:20]
                if isinstance(ar, dict)]
        rows = [r for r in rows if r]
        if not rows:
            return ""
        return "[PREVIOUS ROUND RESULTS]\n" + "\n".join(rows)


def _format_server_action_results(action_results):
    """Format server-dispatched RUN/READ results into [PREVIOUS ROUND RESULTS].

    Mirrors the browser continuation contract so a cloud follow-up pass can
    synthesize a human answer from ground truth instead of echoing raw `$ ...`
    output back to the operator.
    """
    if not isinstance(action_results, list) or not action_results:
        return ""
    try:
        _scripts_on_path()
        import typed_actions as _ta
        envelopes = []
        for idx, row in enumerate(action_results[:20], 1):
            if not isinstance(row, dict):
                continue
            try:
                observed_text = row.get("observed_text")
                if isinstance(observed_text, str) and len(observed_text) > 240:
                    observed_text = observed_text[:240]
                env = _ta.ActionResult(
                    action_id=str(row.get("action_id") or f"server-{idx}"),
                    kind=str(row.get("kind") or "").upper(),
                    target=str(row.get("target") or "")[:300],
                    status=str(row.get("status") or _ta.ResultStatus.SUCCESS),
                    executed=bool(row.get("executed", True)),
                    mode_at_emission="auto",
                    error_code=(str(row.get("error_code") or "")[:120] or None),
                    error_message=(str(row.get("error_message") or "")[:300] or None),
                    observed_text=observed_text if isinstance(observed_text, str) and observed_text else None,
                    gated_by=(str(row.get("gated_by") or "")[:160] or None),
                )
                envelopes.append(_ta.format_envelope_row(env))
            except Exception:
                kind = str(row.get("kind") or "").upper()
                target = str(row.get("target") or "")[:300]
                status = str(row.get("status") or "success")
                executed = "true" if row.get("executed", True) else "false"
                lines = [f"  · {kind} {target}", f"    status: {status}  executed: {executed}"]
                if row.get("error_code"):
                    lines.append(f"    error_code: {row['error_code']}")
                if row.get("error_message"):
                    lines.append(f"    error_message: {str(row['error_message'])[:300]}")
                if row.get("observed_text"):
                    lines.append(f"    observed_text: {str(row['observed_text'])[:240]}")
                envelopes.append("\n".join(lines))
        if not envelopes:
            return ""
        return "[PREVIOUS ROUND RESULTS]\n" + "\n".join(envelopes)
    except Exception:
        rows = []
        for row in action_results[:20]:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "").upper()
            target = str(row.get("target") or "")[:300]
            status = str(row.get("status") or "success")
            executed = "true" if row.get("executed", True) else "false"
            lines = [f"  · {kind} {target}", f"    status: {status}  executed: {executed}"]
            if row.get("error_code"):
                lines.append(f"    error_code: {row['error_code']}")
            if row.get("error_message"):
                lines.append(f"    error_message: {str(row['error_message'])[:300]}")
            if row.get("observed_text"):
                lines.append(f"    observed_text: {str(row['observed_text'])[:240]}")
            rows.append("\n".join(lines))
        if not rows:
            return ""
        return "[PREVIOUS ROUND RESULTS]\n" + "\n".join(rows)


def _cloud_followup_provider(model_name):
    model_name = (model_name or "").strip()
    if model_name in {
        "groq", "fireworks", "cerebras", "deepseek-r1", "gemini",
        "hermes-405b", "gpt-oss-120b", "nemotron", "qwen3-coder",
        "openrouter", "openai", "anthropic",
    }:
        return model_name
    return "groq"


def _synthesize_server_followup(module, history, *, reply_with_results, server_results_block, provider):
    """Ask the same cloud lane to turn server RUN/READ results into operator text.

    Uses a copied message list so the synthetic follow-up prompt does not pollute
    persisted history. The caller decides what final assistant text to save.
    """
    if not server_results_block:
        return ""
    synth_prompt = (
        "[SERVER RESULT SYNTHESIS]\n"
        "The previous assistant reply for this SAME user turn emitted server-side directives "
        "that have now completed. Treat [PREVIOUS ROUND RESULTS] as ground truth.\n"
        "If the user's goal is satisfied, answer in 1-2 plain-language sentences and end with "
        "`DONE: <one-line summary>`.\n"
        "If browser work is next, emit the needed BROWSER_* directives after a one-line lead-in.\n"
        "Do NOT emit RUN, RUNTERM, READ, CREATE, or EDIT in this follow-up pass.\n"
        "Do NOT dump raw shell or file output verbatim unless exact text is necessary.\n\n"
        f"{server_results_block}"
    )
    messages = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        messages.append({
            "role": msg.get("role"),
            "content": msg.get("content"),
        })
    if messages and messages[-1].get("role") == "assistant":
        messages[-1]["content"] = reply_with_results
    messages.append({"role": "user", "content": synth_prompt})
    try:
        return module.ask_cloud(messages, provider=provider) or ""
    except Exception:
        return ""


def _audit_approve_action(action_id, action, verdict, envelope):
    """Write a per-action audit row for /extension/approve_action dispatches.

    Mirrors the existing /extension/action_result audit shape but tags
    `kind: extension_approve_action` so analytics can distinguish backend-
    dispatched approvals from browser-dispatched outcomes.
    """
    try:
        from pathlib import Path as _P
        import time as _t
        rec = {
            'ts': _t.strftime('%Y-%m-%dT%H:%M:%S'),
            'source': 'extension',
            'kind': 'extension_approve_action',
            'action_id': action_id,
            'verdict': verdict,
            'envelope': envelope,
            'action': action,
        }
        with _P.home().joinpath('.master_ai_audit_typed.jsonl').open('a') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass


def _format_action_result_legacy(ar):
    """Original compact format. Kept as a fallback when the typed envelope
    can't be constructed (older side_panel builds, malformed rows, etc.)."""
    if not isinstance(ar, dict):
        return ""
    kind = str((ar.get("action") or {}).get("kind") or ar.get("kind") or "").upper()
    target = str((ar.get("action") or {}).get("target") or ar.get("target") or "")[:300]
    verdict = str(ar.get("verdict") or "").lower()
    result = str(ar.get("result") or "").lower()
    final_state = ar.get("final_state") or {}
    detail = ""
    if isinstance(final_state, dict):
        for key in ("error", "text", "value", "navigated", "reason"):
            v = final_state.get(key)
            if v:
                detail = f" — {key}: {_safe_context_text(str(v), 240)}"
                break
    gated_by = ar.get("gated_by") if isinstance(ar.get("gated_by"), str) else None
    gated = f" (gated by {gated_by})" if gated_by else ""
    return f"  · {kind} {target!r} → verdict={verdict} result={result}{gated}{detail}"


def _fallback_action(kind, target, *, model="", source_text="", cwd=None):
    kind = (kind or "").upper()
    target = (target or "").strip()
    if kind == "BROWSER_SCREENSHOT" and not target:
        target = "viewport"
    if kind in ("BROWSER_READ_PAGE", "BROWSER_OBSERVE") and not target:
        target = "current"
    if not kind or not target:
        return None
    risk = "safe" if kind in (
        "READ", "BROWSER_READ", "BROWSER_READ_PAGE", "BROWSER_OBSERVE",
        "BROWSER_SCREENSHOT", "BROWSER_WAIT", "BROWSER_SCROLL", "BROWSER_EXTRACT_LIST",
    ) else "normal"
    return {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "target": target,
        "cwd": cwd,
        "risk": risk,
        "requires_confirm": True,
        "timeout_s": 60,
        "created_by_model": model or "",
        "source_text": source_text or f"{kind}: {target}",
        "parsed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "pending_approval",
        "extras": {},
    }


# ── Dispatcher-owned sensitivity classifier (M9.x) ────────────────────────
# The model proposes; the dispatcher decides. Static hard gates apply
# regardless of mode. In Auto, safe→runs, sensitive→downgrade to
# waiting_for_approval, blocked→never reaches extension. Reused by
# /extension/approve_action (commit 1.4) and the auto-mode dispatch block
# at line 903+. Per the no-synth-routes-as-skills rule the model never
# self-declares safety; this helper is authoritative.

_SENSITIVE_AUTH_RE = re.compile(
    r"(?i)(?:^|[^a-z])(sign[ _-]?in|sign[ _-]?up|log[ _-]?in|log[ _-]?out|"
    r"password|passwd|2fa|two[ _-]?factor|oauth|api[._-]?key|grant[ ._-]access|"
    r"credentials)"
)
_SENSITIVE_PAYMENT_RE = re.compile(
    r"(?i)(?:^|[^a-z])(checkout|cart|pay(?:ment)?|order|buy|purchase|"
    r"credit[ ._-]?card|debit[ ._-]?card|cvv|cvc|routing[ ._-]?number|"
    r"bank[ ._-]?account|billing)"
)
_SENSITIVE_DESTRUCTIVE_RE = re.compile(
    r"(?i)(?:^|[^a-z])(delete|remove|destroy|erase|wipe|discard|"
    r"cancel[ ._-]+(?:account|subscription)|deactivate)"
)
_SENSITIVE_PASSWORD_INPUT_RE = re.compile(r'(?i)(input\[type="?password"?\]|name=["\']password["\'])')


def _classify_action_sensitivity(action, *, mode, page_url=None):
    """Return {tier: safe|sensitive|blocked, gated_by, error_code}.

    Dispatcher-owned. The model can describe intent; this helper decides
    whether the dispatcher executes (safe), downgrades to approval card
    (sensitive), or refuses outright (blocked). Reused for BROWSER_* and
    for the safe/sensitive split inside chrome_extension auto-mode.
    """
    kind = str(action.get("kind") or "").upper()
    target = str(action.get("target") or "")

    # BLOCKED tier: things that never reach the extension.
    if kind in ("RUN", "RUNTERM"):
        # Reuse typed_actions risk classification + the existing master_ai
        # is_blocked patterns via the registry path; here we only short-
        # circuit for the obviously catastrophic patterns the registry
        # would refuse anyway.
        # NOTE: no trailing \b on the rm pattern — `/` isn't a word char, so
        # `\brm\s+-[rRfF]+\s+/\b` would never match `rm -rf /`. Reuse the
        # shape from typed_actions._HIGH_RISK_RUN_PATTERNS instead.
        if re.search(r"\brm\s+-[rRfF]+[a-zA-Z]*\s+/|\bmkfs\b|\bdd\s+if=.*\bof=/dev/(sd|nvme)", target):
            return {"tier": "blocked", "gated_by": "destructive_run", "error_code": "dispatcher_blocked"}
        if re.search(r"\bcurl\s+[^|]*\|\s*(?:bash|sh)\b|\bwget\s+[^|]*\|\s*(?:bash|sh)\b", target):
            return {"tier": "blocked", "gated_by": "pipe_to_shell", "error_code": "dispatcher_blocked"}

    # SENSITIVE tier: downgrade to waiting_for_approval even in Auto.
    if kind == "BROWSER_CLICK":
        if _SENSITIVE_AUTH_RE.search(target):
            return {"tier": "sensitive", "gated_by": "auth_click", "error_code": None}
        if _SENSITIVE_PAYMENT_RE.search(target):
            return {"tier": "sensitive", "gated_by": "payment_click", "error_code": None}
        if _SENSITIVE_DESTRUCTIVE_RE.search(target):
            return {"tier": "sensitive", "gated_by": "destructive_click", "error_code": None}

    if kind == "BROWSER_FILL":
        if _SENSITIVE_PASSWORD_INPUT_RE.search(target):
            return {"tier": "sensitive", "gated_by": "password_fill", "error_code": None}
        # File upload via the file:// extension we'll add in commit 2.1.
        # Treat any file:// fill as sensitive — uploads cross a trust boundary.
        if "file://" in target.lower() or "file://" in str(action.get("extras", {})).lower():
            return {"tier": "sensitive", "gated_by": "file_upload", "error_code": None}

    if kind == "BROWSER_UPLOAD_FILE":
        return {"tier": "sensitive", "gated_by": "file_upload", "error_code": None}

    if kind == "BROWSER_SUBMIT":
        return {"tier": "sensitive", "gated_by": "form_submit", "error_code": None}

    if kind == "BROWSER_NAV":
        # Navigating into a payment/auth host is sensitive even if the URL
        # text doesn't contain the keyword (e.g. accounts.google.com).
        if re.search(r"(?i)(accounts\.google|login\.|signin\.|auth\.)", target):
            return {"tier": "sensitive", "gated_by": "auth_nav", "error_code": None}
        if re.search(r"(?i)(checkout\.|billing\.|payments?\.)", target):
            return {"tier": "sensitive", "gated_by": "payment_nav", "error_code": None}

    if kind == "BROWSER_CDP_MOUSE":
        # Pixel-coord clicks can't be statically inspected for what they hit;
        # the target is just "x,y". Gate on the active page URL instead.
        # NON-NEGOTIABLE: no auto-mode override, no prompt-side switch.
        url_lc = (page_url or "").lower()
        if re.search(r"(/apply|/checkout|/submit|/share|/delete)\b", url_lc):
            return {"tier": "sensitive", "gated_by": "cdp_mouse_on_sensitive_url", "error_code": None}
        if re.search(r"(?i)(accounts\.google|login\.|signin\.|auth\.|checkout\.|billing\.|payments?\.)", url_lc):
            return {"tier": "sensitive", "gated_by": "cdp_mouse_on_auth_or_payment_host", "error_code": None}
        if "drive.google.com" in url_lc and ("/share" in url_lc or "sharingdialog" in url_lc):
            return {"tier": "sensitive", "gated_by": "drive_share_surface", "error_code": None}

    if kind == "REMOTE_MCP":
        return {"tier": "sensitive", "gated_by": "remote_mcp", "error_code": None}

    if kind in ("CREATE", "EDIT"):
        # File writes outside the active approved cwd list. Use the path
        # as-is; master_ai's _cwd_fence_ok will refuse at dispatch time
        # too, but surfacing it here lets Review show the gated_by reason.
        try:
            expanded = os.path.expanduser(target)
            home = os.path.expanduser("~")
            # CREATE/EDIT on system paths, /etc, /usr, etc.: sensitive at least.
            if expanded.startswith("/etc/") or expanded.startswith("/usr/") or expanded.startswith("/var/"):
                return {"tier": "sensitive", "gated_by": "system_path_write", "error_code": None}
            # Writing in user's repo / scripts dir is normal; flag only if outside both home and /tmp.
            if not (expanded.startswith(home) or expanded.startswith("/tmp/") or expanded.startswith("/var/tmp/")):
                return {"tier": "sensitive", "gated_by": "out_of_home_write", "error_code": None}
        except Exception:
            pass

    if kind in ("RUN", "RUNTERM"):
        # Arbitrary RUN that doesn't start with a known safe prefix is
        # sensitive in Auto. The capability registry handles the typed
        # path; this is the fallback for legacy/untyped commands.
        try:
            _scripts_on_path()
            import typed_actions as _ta
            safe = any(target.strip().startswith(p) for p in _ta._SAFE_RUN_PREFIXES)
            if not safe:
                return {"tier": "sensitive", "gated_by": "arbitrary_run", "error_code": None}
        except Exception:
            return {"tier": "sensitive", "gated_by": "arbitrary_run", "error_code": None}

    return {"tier": "safe", "gated_by": None, "error_code": None}


_5WH_DIRECTIVE_PREFIXES = (
    "RUN:", "RUNTERM:", "READ:", "CREATE:", "EDIT:", "REMEMBER:",
    "BROWSER_CLICK:", "BROWSER_FILL:", "BROWSER_NAV:", "BROWSER_WAIT:",
    "BROWSER_SCROLL:", "BROWSER_READ_PAGE:", "BROWSER_SUBMIT:",
    "BROWSER_SCREENSHOT:", "BROWSER_OBSERVE:", "BROWSER_READ:",
    "BROWSER_DOUBLE_CLICK:", "BROWSER_FIND:", "BROWSER_EXTRACT_LIST:",
)


def _extract_why_for_action(reply_text, action_kind):
    """Pull a one-line rationale from the assistant reply preceding the directive.

    Walk backward from the directive line, skip other directive lines, return
    the first prose sentence found. Empty string if no candidate. Cap ~240 chars.
    """
    if not reply_text or not action_kind:
        return ""
    upper_kind = action_kind.upper()
    lines = reply_text.splitlines()
    for i, line in enumerate(lines):
        if upper_kind not in line.upper():
            continue
        for j in range(i - 1, max(-1, i - 5), -1):
            prev = lines[j].strip()
            if not prev:
                continue
            if any(prev.upper().startswith(p) for p in _5WH_DIRECTIVE_PREFIXES):
                continue
            cleaned = prev.lstrip("-*• 0123456789.")
            if not cleaned:
                continue
            return (cleaned[:237] + "...") if len(cleaned) > 240 else cleaned
        break
    return ""


def _synthesize_5wh(action_kind, target, reply_text, page_url):
    """Per-tool 5W+H card for chrome_extension actions.

    Per feedback_who_what_where_cascade — walk the cascade per case.
    Server is the single source of truth so the model never invents the card
    and the extension never has to guess.
    """
    kind = (action_kind or "").upper()
    where_base = page_url or "current tab"
    who = "Pupil agent on your behalf"
    target = target or ""

    if kind == "BROWSER_CLICK":
        what = "click element"
        where = f"{target} on {where_base}"
        how = "synthetic click event via content script"
    elif kind == "BROWSER_FILL":
        sep = re.search(r"^(.+?)\s*(?:::|=>|:=)\s*(.+)$", target)
        sel = sep.group(1).strip() if sep else target
        what = "type text into form field"
        where = f"{sel} on {where_base}"
        how = "synthetic input + change events via content script"
    elif kind == "BROWSER_NAV":
        what = "navigate active tab"
        where = f"to {target}"
        how = "chrome.tabs.update with new URL"
    elif kind == "BROWSER_WAIT":
        what = "wait for page rendering"
        where = f"{target}ms"
        how = "setTimeout in content script"
    elif kind == "BROWSER_SCROLL":
        what = "scroll page"
        where = f"{target} on {where_base}"
        how = "window.scrollBy / scrollIntoView via content script"
    elif kind == "BROWSER_READ_PAGE":
        what = "observe current page (DOM + a11y tree)"
        where = where_base
        how = "DOM query + accessibility snapshot via content script"
    elif kind == "BROWSER_SUBMIT":
        what = "submit form"
        where = f"{target} on {where_base}"
        how = "form.submit() via content script"
    elif kind == "BROWSER_SCREENSHOT":
        what = "capture viewport screenshot"
        where = where_base
        how = "chrome.tabs.captureVisibleTab"
    else:
        what = (action_kind or "").lower().replace("_", " ") or "action"
        where = where_base
        how = "via content script"

    why = _extract_why_for_action(reply_text or "", action_kind or "") or "as proposed by Pupil agent"
    return {"who": who, "what": what, "where": where, "why": why, "how": how}


def _api_parse_actions(reply, *, mode="plan", model="", source="", session_id="", schedule_id="", page_context=None):
    actions = []
    seen = set()
    mode_norm = (mode or "plan").lower()
    if mode_norm not in ("plan", "review", "auto"):
        mode_norm = "plan"
    page_url = None
    if isinstance(page_context, dict):
        page_url = page_context.get("url")

    def add(action):
        if not isinstance(action, dict):
            return
        kind = str(action.get("kind") or "").upper()
        target = str(action.get("target") or "").strip()
        if kind == "BROWSER_SCREENSHOT" and not target:
            target = "viewport"
            action["target"] = target
        if not kind or not target:
            return
        key = (kind, target)
        if key in seen:
            return
        seen.add(key)
        action["kind"] = kind
        action["target"] = target

        # Dispatcher-owned sensitivity. Always computed; mode decides how
        # to use it. Note: blocked actions are dropped entirely below;
        # they never reach the extension.
        sens = _classify_action_sensitivity(action, mode=mode_norm, page_url=page_url)
        action["sensitivity_tier"] = sens["tier"]
        if sens["gated_by"]:
            action["gated_by"] = sens["gated_by"]

        if sens["tier"] == "blocked":
            # Never expose blocked actions to the extension; they're
            # surfaced to the user via the reply text + audit log only.
            return

        # Mode → status taxonomy. ResultStatus values from typed_actions.
        if mode_norm == "plan":
            action["status"] = "planned"
            action["executed"] = False
            action["requires_confirm"] = False  # inert preview, no buttons
            action["mode_at_emission"] = "plan"
        elif mode_norm == "review":
            action["status"] = "waiting_for_approval"
            action["executed"] = False
            action["requires_confirm"] = True
            action["mode_at_emission"] = "review"
        else:  # auto
            if sens["tier"] == "sensitive":
                action["status"] = "waiting_for_approval"
                action["executed"] = False
                action["requires_confirm"] = True
            else:  # safe
                action["status"] = "planned"  # about to run; side_panel auto-runs
                action["executed"] = False
                action["requires_confirm"] = False
            action["mode_at_emission"] = "auto"

        # Preserve legacy field name for back-compat with older side_panel
        # builds that read action.status == "pending_approval".
        if action["status"] == "waiting_for_approval":
            action.setdefault("legacy_status", "pending_approval")

        action.setdefault("id", str(uuid.uuid4()))
        action.setdefault("created_by_model", model or "")
        action.setdefault("parsed_at", datetime.now().astimezone().isoformat(timespec="seconds"))
        extras = action.get("extras") if isinstance(action.get("extras"), dict) else {}
        extras.update({
            "api_branch": "B",
            "source": source or "pupil",
        })
        if session_id:
            extras["session_id"] = session_id
        if schedule_id:
            extras["schedule_id"] = schedule_id
        if isinstance(page_context, dict) and page_context.get("url"):
            extras["page_url"] = _safe_context_text(page_context.get("url"), 500)
        action["extras"] = extras
        if source == "chrome_extension":
            action["_5wh"] = _synthesize_5wh(action["kind"], action["target"], reply or "", page_url)
        actions.append(action)

    try:
        _scripts_on_path()
        import typed_actions as _ta
        for parsed in _ta.parse_reply(reply or "", model=model or "", cwd=os.getcwd()):
            try:
                add(parsed.to_dict())
            except Exception:
                pass
    except Exception:
        pass

    for raw in (reply or "").splitlines():
        m = _ACTION_LINE_RE.match(raw)
        if not m:
            continue
        action = _fallback_action(
            m.group(1),
            m.group(2),
            model=model or "",
            source_text=raw,
            cwd=os.getcwd(),
        )
        add(action)

    return actions


def _tool_find(payload):
    """Phase 7: semantic browser find over the compact AX tree.

    The extension sends the active page's accessibility snapshot. This endpoint
    routes through subagent_registry so the find implementation stays swappable
    without adding another dispatch surface.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    query = str(payload.get("query") or "").strip()
    if not query:
        raise ValueError("missing query")
    ax_tree = payload.get("ax_tree") or payload.get("tree") or {}
    if not isinstance(ax_tree, dict):
        ax_tree = {}
    _scripts_on_path()
    import subagent_registry as _sr
    result = _sr.run("find", query, context={"ax_tree": ax_tree})
    matches = result.get("matches") if isinstance(result, dict) else []
    if not isinstance(matches, list):
        matches = []
    clean = []
    for item in matches[:20]:
        if not isinstance(item, dict):
            continue
        clean.append({
            "ref": _safe_context_text(item.get("ref"), 80),
            "name": _safe_context_text(item.get("name"), 240),
            "role": _safe_context_text(item.get("role"), 80),
            "selector": _safe_context_text(item.get("selector"), 300),
            "confidence": float(item.get("confidence") or 0.0),
        })
    return {
        "ok": True,
        "query": _safe_context_text(query, 240),
        "matches": clean,
        "count": len(clean),
        "subagent": "find",
    }


def _tool_describe_step(payload):
    """Phase 9.3: one-line labels for recorded workflow steps."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    step = payload.get("step") if isinstance(payload.get("step"), dict) else {}
    transcript = _safe_context_text(payload.get("transcript") or "", 600)
    _scripts_on_path()
    import subagent_registry as _sr
    result = _sr.run("workflow_describer", "", context={
        "step": step,
        "transcript": transcript,
    })
    desc = ""
    if isinstance(result, dict):
        desc = result.get("description") or ""
    if not desc:
        kind = _safe_context_text(step.get("kind") or step.get("type") or "step", 80)
        target = _safe_context_text(step.get("target") or step.get("selector") or "", 120)
        desc = f"{kind} {target}".strip()
    return {
        "ok": True,
        "description": _safe_context_text(desc, 240),
        "subagent": "workflow_describer",
    }


_DONE_DIRECTIVE_RE = re.compile(r"^\s*DONE:\s*\S")


def _reply_has_done_directive(reply):
    """Return True if `reply` contains an explicit `DONE: <summary>` line.

    M9.1 termination signal: when the model says it's finished, the agent loop
    ends with terminal_reason="done_directive" — taking priority over the
    implicit no_actions / budget terminal branches. The line must be at column
    0 (whitespace allowed), match `DONE:` verbatim (parser-style), and have at
    least one non-whitespace character after the colon (rejects bare `DONE:`).
    """
    if not reply or not isinstance(reply, str):
        return False
    for line in reply.splitlines():
        if _DONE_DIRECTIVE_RE.match(line):
            return True
    return False


def _api_terminal_state(reply, captured_actions, round_remaining, server_progressed=False):
    """Return (done, terminal_reason) for an API turn."""
    if captured_actions and round_remaining > 0:
        return False, ""
    if captured_actions and round_remaining <= 0:
        return True, "budget"
    if _reply_has_done_directive(reply):
        return True, "done_directive"
    if server_progressed and round_remaining > 0:
        # Server-side dispatch fired (RUN/RUNTERM/READ/CREATE/EDIT) but the
        # model didn't emit DONE and didn't emit BROWSER_* actions for the
        # extension. Keep the turn open so the extension fires
        # /chat/continue with empty action_results, giving the model another
        # round to react to the server output and emit the next planned
        # step. Required for multi-lane workflows where a terminal step
        # precedes a browser step.
        return False, "server_progressed"
    return True, "no_actions"


def _api_blocked_actions(module, extra_blocked=None):
    out = []
    for blocked in (extra_blocked or []):
        if isinstance(blocked, dict):
            out.append(blocked)
    blocked = getattr(module, "_LAST_BLOCKED_ACTION", {}) or {}
    if isinstance(blocked, dict) and blocked:
        item = {
            "kind": str(blocked.get("kind") or "").upper(),
            "target": blocked.get("command") or blocked.get("path") or "",
            "reason": blocked.get("reason") or "blocked by Sensei policy",
        }
        if blocked.get("audit_kind"):
            item["audit_kind"] = blocked.get("audit_kind")
        out.append(item)
    return out


def api_handle(payload):
    """Non-interactive /chat bridge into master_ai.handle().

    Branch B contract: the backend can propose typed actions, but it must not
    call the TUI confirmation/execution path. The Chrome extension confirms and
    dispatches browser actions separately, then reports results back.

    M9 (2026-05-12): supports continuation rounds. If payload includes
    parent_turn_id + action_results, the previous round's outcomes are
    formatted into [PREVIOUS ROUND RESULTS] and the model decides whether to
    propose more actions or reply with a final answer (empty actions[] = done).
    Round budget caps the loop.
    """
    payload = payload if isinstance(payload, dict) else {}
    # Accept either `prompt` (canonical / Pupil web) or `user_input` (Pupil
    # Hands chrome_extension contract). Same semantic, different field name
    # so the extension request shape matches the goal-spec verbatim.
    prompt = (payload.get("prompt") or payload.get("user_input") or "").strip()
    if not prompt:
        raise ValueError("missing prompt")
    mode_req = (payload.get("mode") or "").strip().lower()
    if mode_req and mode_req not in ("plan", "review", "auto", "quick"):
        raise ValueError("invalid mode")
    # Compute the effective mode once. Used by _api_parse_actions to set
    # status taxonomy (planned/waiting_for_approval/running) and by the
    # chrome_extension auto-mode dispatch block below. Don't recompute
    # later — that risks drift if _m.MODE shifts during handle().
    #
    # _m lookup uses sys.modules instead of a local `_m` reference because
    # `import master_ai as _m` lower in this function makes `_m` a Python-
    # local symbol throughout — referencing it here pre-import would fire
    # UnboundLocalError. Hit live 2026-05-14 e2e smoke via /chat/continue
    # when the extension omitted `mode` in the continuation payload.
    _m_module_for_mode = sys.modules.get("master_ai")
    _module_mode_default = getattr(_m_module_for_mode, "MODE", "plan") if _m_module_for_mode else "plan"
    effective_mode = (mode_req or _module_mode_default or "plan").lower()
    if effective_mode not in ("plan", "review", "auto", "quick"):
        effective_mode = "plan"

    source_raw = payload.get("source")
    source = _safe_context_text(source_raw or "pupil", 80)
    prompt_source = _safe_context_text(source_raw or "", 80)
    session_id = _safe_context_text(payload.get("session_id") or "", 160)
    schedule_id = _safe_context_text(payload.get("schedule_id") or "", 120)
    page_context = payload.get("page_context") if isinstance(payload.get("page_context"), dict) else None
    # Phase 4.3 — tabs_context: list of currently-open tabs the extension
    # wants the model to know about (typically the session group's tabs).
    tabs_context = payload.get("tabs_context") if isinstance(payload.get("tabs_context"), list) else None
    requested_model = (payload.get("model") or "").strip()
    t0 = time.time()

    # M9 continuation context
    parent_turn_id = _safe_context_text(payload.get("parent_turn_id") or "", 80)
    action_results = payload.get("action_results") if isinstance(payload.get("action_results"), list) else None
    # Browser-automation turns get a higher default + ceiling so real
    # multi-step page flows (Drive search → open → read; checkout walk;
    # job-app fill+submit) don't false-terminate. See
    # _API_BROWSER_DEFAULT_ROUND_BUDGET comment.
    _is_browser_turn = bool(
        source_raw and "chrome_extension" in str(source_raw).lower()
        and page_context
    )
    _budget_default = (_API_BROWSER_DEFAULT_ROUND_BUDGET if _is_browser_turn
                       else _API_DEFAULT_ROUND_BUDGET)
    _budget_max = (_API_BROWSER_MAX_ROUND_BUDGET if _is_browser_turn
                   else _API_MAX_ROUND_BUDGET)
    try:
        req_budget = int(payload.get("round_budget") or _budget_default)
    except (TypeError, ValueError):
        req_budget = _budget_default
    req_budget = max(1, min(req_budget, _budget_max))

    # M9 safety: detect duplicate-target failures in the incoming round and
    # short-circuit before burning another model turn. If the same target
    # failed twice in the latest action_results batch, the model is almost
    # certainly going to retry the same selector. Force termination instead.
    short_circuit_reason = _duplicate_failure_reason(action_results)

    # Resolve turn lineage. Continuation references parent; fresh /chat mints new.
    if parent_turn_id:
        with _API_TURNS_LOCK:
            parent_meta = dict(_API_TURNS.get(parent_turn_id, {}))
        if parent_meta:
            round_budget = int(parent_meta.get("round_budget") or req_budget)
            round_num = int(parent_meta.get("round_num", 1)) + 1
            turn_root = parent_meta.get("turn_root") or parent_turn_id
        else:
            # Unknown parent — treat as fresh turn but echo the id back to caller.
            round_budget = req_budget
            round_num = 1
            turn_root = parent_turn_id
    else:
        round_budget = req_budget
        round_num = 1
        turn_root = None

    turn_id = str(uuid.uuid4())

    # M9 short-circuit: same-target-twice-failed in this round → terminate
    # without calling the model. Returns a synthetic done=true response.
    if short_circuit_reason:
        return _build_terminal_turn(
            turn_id=turn_id,
            turn_root=turn_root or turn_id,
            round_num=round_num,
            round_budget=round_budget,
            parent_turn_id=parent_turn_id,
            session_key=_api_session_key(source, session_id),
            reply=f"[loop terminated: {short_circuit_reason}]",
            route="loop_safety",
            model="m9_short_circuit",
            t0=t0,
            terminal_reason="duplicate_failure",
        )

    session_key = _api_session_key(source, session_id)
    with _API_HISTORY_LOCK:
        history = list(_API_HISTORIES.get(session_key, [])) if session_key else []

    api_user_text = _api_prompt(
        prompt,
        source=prompt_source,
        page_context=page_context,
        schedule_id=schedule_id,
        action_results=action_results,
        round_num=round_num,
        round_budget=round_budget,
        request_id=turn_id,
        resume_path=str(payload.get("resume_path") or "").strip(),
        local_file_hints=payload.get("local_file_hints") if isinstance(payload.get("local_file_hints"), dict) else None,
        tabs_context=tabs_context,
        mode=effective_mode,
    )

    lane = _predict_api_handle_lane(history, api_user_text, requested_model=requested_model)
    lane_lock = _API_HANDLE_LOCKS.get(lane, _API_HANDLE_LOCKS["local"])

    # Timed acquire instead of blocking `with` — see _API_HANDLE_LOCK_TIMEOUT_S
    # comment near the lane-lock definition. Without this, a runaway request
    # can wedge its own lane for minutes. Different lanes run through separate
    # imported master_ai module copies, so Groq/DeepSeek traffic no longer
    # waits behind a local Ollama inference.
    if not lane_lock.acquire(timeout=_API_HANDLE_LOCK_TIMEOUT_S):
        raise ApiHandleBusy(
            f"timed out after {_API_HANDLE_LOCK_TIMEOUT_S:.0f}s waiting for "
            f"/chat dispatch lock for lane {lane}; likely runaway inference in that lane. "
            f"Retry after the current request completes."
        )
    try:
        _scripts_on_path()
        _m = _get_api_master_ai_module(lane)
        import capabilities as _caps
        import verifiers as _verifiers  # noqa: F401  -- resolved lazily by Capability.resolve_verifier(), imported here so import errors surface at request time, not mid-dispatch
        import prompt_versions as _pv

        captured_actions = []
        captured_blocked = []
        patches = []
        # Registry-handled actions get aggregated here for audit-row derivation
        # AFTER the with-block. Each entry is the full _action dict carrying
        # capability + verification_result metadata. captured_actions is
        # reassigned to _browser_only mid-dispatch, so registry actions need
        # their own sibling list to survive that reassignment.
        _registry_handled = []
        prev_mode = getattr(_m, "MODE", "plan")
        prev_pinned = getattr(_m, "PINNED_MODEL", None)

        def patch(name, value):
            if hasattr(_m, name):
                patches.append((name, getattr(_m, name)))
                setattr(_m, name, value)

        # Wedge protection: cap local Ollama urlopen below the lock timeout.
        # The 600s default in master_ai.ask_local / ask_local_stream is for
        # TUI Plan-mode breathing room; /chat dispatch must die BEFORE the
        # lock-acquire timeout fires, otherwise the same wedge greets every
        # follow-up request and burns another 120s. Slack of 10s ensures the
        # urlopen raises first and the lock releases cleanly.
        patch("LOCAL_REQUEST_TIMEOUT_OVERRIDE", max(10, int(_API_HANDLE_LOCK_TIMEOUT_S) - 10))

        def collect_only(reply, history_ref, streamed=False, continue_after_tools=False):
            nonlocal captured_actions
            model = getattr(_m, "LAST_MODEL", "") or requested_model or "master-ai"
            captured_actions = _api_parse_actions(
                reply or "",
                mode=effective_mode,
                model=model,
                source=source,
                session_id=session_id,
                schedule_id=schedule_id,
                page_context=page_context,
            )
            return reply

        def noninteractive_confirm(cmd="", *args, **kwargs):
            detail = cmd
            if not detail and args:
                detail = args[0]
            captured_blocked.append({
                "kind": "ACTION",
                "target": str(detail or "")[:1000],
                "reason": "api_handle is non-interactive; action returned for extension confirmation",
            })
            try:
                return _m.RunResult(
                    "Blocked: API requests do not execute TUI-confirmed actions.",
                    ok=False,
                    exit_code=None,
                    command=str(detail or ""),
                    error="api_non_interactive",
                )
            except Exception:
                return False

        def noninteractive_create(filepath, content="", *args, **kwargs):
            captured_blocked.append({
                "kind": "CREATE",
                "target": str(filepath or "")[:1000],
                "reason": "api_handle is non-interactive; create action returned for confirmation",
            })
            return False

        def noninteractive_edit(filepath, find_text="", replace_text="", *args, **kwargs):
            captured_blocked.append({
                "kind": "EDIT",
                "target": str(filepath or "")[:1000],
                "reason": "api_handle is non-interactive; edit action returned for confirmation",
            })
            return False

        try:
            patch("process_reply", collect_only)
            patch("confirm_run", noninteractive_confirm)
            patch("confirm_runterm", noninteractive_confirm)
            patch("confirm_create", noninteractive_create)
            patch("confirm_edit", noninteractive_edit)
            patch("_try_desktop_open_intent", lambda user_text: None)
            patch("_try_open_url_intent", lambda user_text: None)
            patch("handle_save_refresh", lambda history_ref: None)
            try:
                _m._LAST_BLOCKED_ACTION = {}
            except Exception:
                pass
            if mode_req:
                _m.MODE = mode_req
            if requested_model and requested_model != "master-ai":
                _m.PINNED_MODEL = requested_model
            reply = _m.handle(api_user_text, history)
            route = getattr(_m, "LAST_ROUTE", "") or "local"
            model = getattr(_m, "LAST_MODEL", "") or requested_model or "master-ai"
            if not captured_actions:
                captured_actions = _api_parse_actions(
                    reply or "",
                    mode=effective_mode,
                    model=model,
                    source=source,
                    session_id=session_id,
                    schedule_id=schedule_id,
                    page_context=page_context,
                )
            blocked_actions = _api_blocked_actions(_m, captured_blocked)

            # Chrome extension auto mode — server-side dispatch for non-BROWSER
            # directives. The extension only knows how to execute BROWSER_*;
            # RUN/RUNTERM/READ/CREATE/EDIT get run here through the real
            # master_ai handlers. Auto-mode gates still apply (policy,
            # cleanup-safety, blocked-patterns, hallucination guard, sudo
            # handoff, TTY-refusal for destructive). BROWSER_* stays in
            # captured_actions for the extension to dispatch on the page;
            # local output gets appended to the reply text.
            # effective_mode is computed early at line 866 — shared with
            # _api_parse_actions so the status taxonomy stays in sync.
            _server_progressed = False
            if source == "chrome_extension" and effective_mode == "auto":
                _real = {n: o for n, o in patches}
                for _name in ("confirm_run", "confirm_runterm", "confirm_create", "confirm_edit"):
                    if _name in _real:
                        setattr(_m, _name, _real[_name])
                _m.MODE = "auto"
                _server_out = []
                _server_results = []
                _browser_only = []
                for _action in captured_actions:
                    _kind = (_action.get("kind") or "").upper()
                    _target = _action.get("target") or ""
                    if _kind.startswith("BROWSER_"):
                        _browser_only.append(_action)
                        continue

                    # Registry consultation BEFORE generic dispatch. Matched
                    # capability owns the execution path (executor + verifier
                    # + audit metadata). Unmatched falls through to legacy
                    # dispatch below — keeps the registry opt-in during
                    # phase 1.
                    _decision = _caps.get_registry().lookup(_kind, _target)
                    if _decision.capability is not None:
                        _cap = _decision.capability
                        _action["capability"] = _cap.name
                        _action["decision_reason"] = _decision.reason
                        # Record at the top of the decision branch so refused,
                        # requires_confirmation, and executed branches all
                        # land in the audit aggregation. The _action dict is
                        # mutated by reference in each branch below, so at
                        # audit time this carries the final state.
                        _registry_handled.append(_action)
                        if not _decision.allow:
                            _server_out.append(f"[{_cap.name}] refused: {_decision.reason}")
                            _action["verification_result"] = None
                            _action["blocked_reason"] = _decision.reason
                            _server_results.append({
                                "kind": _kind,
                                "target": _target,
                                "status": "blocked",
                                "executed": False,
                                "error_code": "dispatcher_blocked",
                                "error_message": _decision.reason,
                                "gated_by": _decision.reason,
                            })
                            continue
                        if _decision.requires_confirmation:
                            # Don't execute server-side; surface as action
                            # card to the extension for confirm UI. Phase 2
                            # wires the actual confirm surface in side_panel.
                            _action["requires_confirmation"] = True
                            _action["risk_tier"] = _cap.risk_tier
                            _browser_only.append(_action)
                            continue
                        try:
                            _executor = _cap.resolve_executor()
                            # Prefer verify_target when the registry extracted
                            # a specific argument (e.g., bare app name for
                            # desktop.launch_app); otherwise pass the raw
                            # directive target.
                            _exec_arg = _decision.verify_target if _decision.verify_target else _target
                            _exec_result = _executor(_exec_arg)
                            _exec_stdout = getattr(_exec_result, "stdout", "") or ""
                            _verifier = _cap.resolve_verifier()
                            _verify_result = _verifier(
                                _decision.verify_target,
                                max_wait_s=_cap.verification_policy.max_wait_s,
                                poll_ms=_cap.verification_policy.poll_ms,
                            )
                            _action["verification_result"] = {
                                "ok": _verify_result.ok,
                                "observed": _verify_result.observed,
                                "elapsed_ms": _verify_result.elapsed_ms,
                                "reason": _verify_result.reason,
                            }
                            _lines = [f"$ {_target}"]
                            if _exec_stdout.strip():
                                _lines.append(_exec_stdout.rstrip())
                            if _verify_result.ok:
                                _lines.append(
                                    f"[{_cap.name}] verified: {_verify_result.observed} "
                                    f"({_verify_result.elapsed_ms}ms)"
                                )
                                _status = "success"
                                _err_msg = None
                            else:
                                _lines.append(
                                    f"[{_cap.name}] not verified: {_verify_result.reason}"
                                )
                                _status = "failure"
                                _err_msg = _verify_result.reason
                            _server_out.append("\n".join(_lines))
                            _server_results.append({
                                "kind": _kind,
                                "target": _target,
                                "status": _status,
                                "executed": True,
                                "error_code": None if _verify_result.ok else "dispatcher_error",
                                "error_message": _err_msg,
                                "observed_text": _exec_stdout or str(_verify_result.observed or ""),
                            })
                        except Exception as _e:
                            _action["verification_result"] = None
                            _action["executor_error"] = str(_e)
                            _server_out.append(
                                f"[{_cap.name}] dispatch error: {type(_e).__name__}: {_e}"
                            )
                            _server_results.append({
                                "kind": _kind,
                                "target": _target,
                                "status": "failure",
                                "executed": True,
                                "error_code": "dispatcher_error",
                                "error_message": f"{type(_e).__name__}: {_e}",
                            })
                        continue

                    try:
                        if _kind == "RUN":
                            _r = _m.confirm_run(_target)
                            if _r is None:
                                _server_out.append(f"$ {_target}\n[blocked or refused]")
                                _server_results.append({
                                    "kind": _kind,
                                    "target": _target,
                                    "status": "blocked",
                                    "executed": False,
                                    "error_code": "dispatcher_blocked",
                                    "error_message": "blocked or refused",
                                })
                            else:
                                _stdout = getattr(_r, "stdout", "") or ""
                                _ok = getattr(_r, "ok", True)
                                _suffix = "" if _ok else " (exit nonzero)"
                                _server_out.append(f"$ {_target}{_suffix}\n{_stdout}".rstrip())
                                _server_results.append({
                                    "kind": _kind,
                                    "target": _target,
                                    "status": "success" if _ok else "failure",
                                    "executed": True,
                                    "error_code": None if _ok else "dispatcher_error",
                                    "error_message": None if _ok else "command exited nonzero",
                                    "observed_text": _stdout,
                                })
                        elif _kind == "RUNTERM":
                            _m.confirm_runterm(_target)
                            _server_out.append(f"[RUNTERM] dispatched: {_target}")
                            _server_results.append({
                                "kind": _kind,
                                "target": _target,
                                "status": "success",
                                "executed": True,
                                "observed_text": "dispatched in a new terminal",
                            })
                        elif _kind == "READ":
                            _expanded = os.path.expanduser(_target)
                            # URL guard (sensei_read_dispatcher_guard.sh, 2026-05-16):
                            # cloud planners have been emitting READ with URLs (browser
                            # content) and unexpanded globs (~/Downloads/*.pdf) that
                            # open() rejects with Errno 2. Surface explicit corrective
                            # guidance through _server_out so the next-turn cloud lane
                            # has something to act on besides a bare "no such file".
                            if _expanded.startswith(("http://", "https://")):
                                _server_out.append(
                                    f"[READ {_target}] blocked: target is a URL. "
                                    f"For browser content, emit:\n"
                                    f"  BROWSER_NAV: {_target}\n"
                                    f"then BROWSER_READ_PAGE or BROWSER_OBSERVE to read it. "
                                    f"READ is only for local filesystem paths."
                                )
                                _server_results.append({
                                    "kind": _kind,
                                    "target": _target,
                                    "status": "blocked",
                                    "executed": False,
                                    "error_code": "dispatcher_blocked",
                                    "error_message": "READ target is a URL, not a local path",
                                })
                                continue
                            if any(_g in _expanded for _g in ("*", "?", "[")):
                                import glob as _glob_mod
                                _matches = sorted(_glob_mod.glob(_expanded))
                                if not _matches:
                                    _dir = _expanded.rsplit('/', 1)[0] if '/' in _expanded else '.'
                                    _server_out.append(
                                        f"[READ {_target}] blocked: glob matched zero files. "
                                        f"Use a specific path or first run:\n"
                                        f"  RUN: ls {_dir}\n"
                                        f"to see what exists, then READ the exact file path."
                                    )
                                    _server_results.append({
                                        "kind": _kind,
                                        "target": _target,
                                        "status": "blocked",
                                        "executed": False,
                                        "error_code": "dispatcher_blocked",
                                        "error_message": "glob matched zero files",
                                    })
                                    continue
                                _expanded = _matches[0]
                                if len(_matches) > 1:
                                    _others = "\n  ".join(_matches[1:5])
                                    _suffix = f" (+ {len(_matches) - 5} more)" if len(_matches) > 5 else ""
                                    _server_out.append(
                                        f"[READ {_target}] glob matched {len(_matches)} files; "
                                        f"reading first: {_expanded}\n"
                                        f"Other matches:\n  {_others}{_suffix}"
                                    )
                            if hasattr(_m, "_read_path_ok"):
                                try:
                                    _read_ok, _reason = _m._read_path_ok(_expanded)
                                except Exception:
                                    _read_ok, _reason = True, ""
                                if not _read_ok:
                                    _server_out.append(f"[READ {_target}] blocked: {_reason}")
                                    _server_results.append({
                                        "kind": _kind,
                                        "target": _target,
                                        "status": "blocked",
                                        "executed": False,
                                        "error_code": "dispatcher_blocked",
                                        "error_message": _reason,
                                    })
                                    continue
                            try:
                                with open(_expanded, "r", errors="replace") as _f:
                                    _content = _f.read()
                                if len(_content) > 4000:
                                    _content = _content[:4000] + f"\n... [truncated, {len(_content)} chars total]"
                                _server_out.append(f"READ {_target}:\n{_content}")
                                _server_results.append({
                                    "kind": _kind,
                                    "target": _target,
                                    "status": "success",
                                    "executed": True,
                                    "observed_text": _content,
                                })
                            except Exception as _e:
                                _server_out.append(f"[READ {_target}] error: {_e}")
                                _server_results.append({
                                    "kind": _kind,
                                    "target": _target,
                                    "status": "failure",
                                    "executed": True,
                                    "error_code": "dispatcher_error",
                                    "error_message": str(_e),
                                })
                        elif _kind == "CREATE":
                            _content_body = _action.get("content") or _action.get("body") or ""
                            _ok = _m.confirm_create(_target, _content_body)
                            _server_out.append(f"[CREATE {_target}] {'written' if _ok else 'blocked or refused'}")
                            _server_results.append({
                                "kind": _kind,
                                "target": _target,
                                "status": "success" if _ok else "blocked",
                                "executed": bool(_ok),
                                "error_code": None if _ok else "dispatcher_blocked",
                                "error_message": None if _ok else "blocked or refused",
                            })
                        elif _kind == "EDIT":
                            _find = _action.get("find") or _action.get("find_text") or ""
                            _replace = _action.get("replace") or _action.get("replace_text") or ""
                            _ok = _m.confirm_edit(_target, _find, _replace)
                            _server_out.append(f"[EDIT {_target}] {'applied' if _ok else 'blocked or refused'}")
                            _server_results.append({
                                "kind": _kind,
                                "target": _target,
                                "status": "success" if _ok else "blocked",
                                "executed": bool(_ok),
                                "error_code": None if _ok else "dispatcher_blocked",
                                "error_message": None if _ok else "blocked or refused",
                            })
                        else:
                            _browser_only.append(_action)
                    except Exception as _e:
                        _server_out.append(f"[{_kind} {_target}] dispatch error: {_e}")
                        _server_results.append({
                            "kind": _kind,
                            "target": _target,
                            "status": "failure",
                            "executed": True,
                            "error_code": "dispatcher_error",
                            "error_message": str(_e),
                        })
                if _server_out:
                    reply_with_results = (reply or "") + "\n\n— server-dispatched output —\n" + "\n\n".join(_server_out)
                    reply = reply_with_results
                    if history and history[-1].get("role") == "assistant":
                        history[-1]["content"] = reply_with_results
                    _server_progressed = True
                    _has_run_read = any(
                        str(_row.get("kind") or "").upper() in ("RUN", "READ")
                        for _row in _server_results
                    )
                    if route in ("cloud", "cloud_fast", "cloud_deep") and not _browser_only and _has_run_read:
                        _server_results_block = _format_server_action_results(_server_results)
                        _synth_reply = _synthesize_server_followup(
                            _m,
                            history,
                            reply_with_results=reply_with_results,
                            server_results_block=_server_results_block,
                            provider=_cloud_followup_provider(model),
                        )
                        if _synth_reply:
                            reply = _synth_reply
                            if history and history[-1].get("role") == "assistant":
                                history[-1]["content"] = _synth_reply
                            _browser_only = _api_parse_actions(
                                reply or "",
                                mode=effective_mode,
                                model=model,
                                source=source,
                                session_id=session_id,
                                schedule_id=schedule_id,
                                page_context=page_context,
                            )
                captured_actions = _browser_only

            try:
                _m._LAST_BLOCKED_ACTION = {}
            except Exception:
                pass
        finally:
            for name, old in reversed(patches):
                try:
                    setattr(_m, name, old)
                except Exception:
                    pass
            try:
                _m.MODE = prev_mode
                _m.PINNED_MODEL = prev_pinned
            except Exception:
                pass
    finally:
        lane_lock.release()

    if session_key:
        with _API_HISTORY_LOCK:
            _API_HISTORIES[session_key] = _trim_api_history(history)

    # M9 termination signal. Browser actions keep the turn open until the
    # extension reports real results; DONE is terminal only with no actions.
    round_remaining = max(0, round_budget - round_num)
    done, terminal_reason = _api_terminal_state(
        reply, captured_actions, round_remaining,
        server_progressed=locals().get("_server_progressed", False),
    )

    # Register this turn so a continuation request can find it.
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    with _API_TURNS_LOCK:
        _API_TURNS[turn_id] = {
            "session_key": session_key,
            "round_num": round_num,
            "round_budget": round_budget,
            "turn_root": turn_root or turn_id,
            "parent_turn_id": parent_turn_id or None,
            "created_at": now_iso,
            "done": done,
        }
        # Bounded registry — drop oldest entries if it grows past 256.
        if len(_API_TURNS) > 256:
            oldest = sorted(_API_TURNS.items(), key=lambda kv: kv[1].get("created_at", ""))[:64]
            for k, _ in oldest:
                _API_TURNS.pop(k, None)

    # M9 turn-level audit on terminal rounds only. Mid-loop rounds (done=false)
    # stay out of the JSONL — only the closing row per turn-root is recorded,
    # so behavioral analytics can query terminal_reason ∈ {done_directive,
    # no_actions, budget, duplicate_failure} per goal without filtering noise.
    if done:
        # Phase 1 audit-column extensions per
        # ~/.claude/plans/reactive-waddling-papert.md:
        # - task_id / correlation_id / causation_id: explicit aliases over the
        #   existing turn_id / turn_root / parent_turn_id so audit queries can
        #   join across the agentic vocabulary the rest of the system uses.
        # - capabilities_fired / verification_results: aggregated from
        #   _registry_handled so a single audit row carries the full evidence
        #   chain for the turn (capability name + full VerifyResult shape:
        #   ok, observed, elapsed_ms, reason). Forward-only: old rows without
        #   these fields stay valid; readers tolerate missing keys.
        # - prompt_version family: content-hash-first stamping from the
        #   system-prompt assembly site in master_ai.py, so behavior can be
        #   correlated to the exact config that produced it.
        try:
            import prompt_versions as _pv_audit
            _pv_dict = _pv_audit.current()
        except Exception:
            _pv_dict = {}
        _write_turn_audit({
            "ts": now_iso,
            "source": "api_handle",
            "kind": "turn_terminal",
            "turn_id": turn_id,
            "task_id": turn_id,
            "turn_root": turn_root or turn_id,
            "correlation_id": turn_root or turn_id,
            "parent_turn_id": parent_turn_id or None,
            "causation_id": parent_turn_id or None,
            "round_num": round_num,
            "round_budget": round_budget,
            "terminal_reason": terminal_reason,
            "route": route,
            "model": model,
            "actions_count": len(captured_actions),
            "blocked_count": len(blocked_actions),
            "capabilities_fired": [a.get("capability") for a in _registry_handled if a.get("capability")],
            "verification_results": [a.get("verification_result") for a in _registry_handled if a.get("verification_result")],
            "prompt_version": _pv_dict.get("prompt_version"),
            "prompt_sha256_short": _pv_dict.get("prompt_sha256_short"),
            "git_commit_short": _pv_dict.get("git_commit_short"),
            "registry_version": _pv_dict.get("registry_version"),
            "safety_policy_version": _pv_dict.get("safety_policy_version"),
        })

    # Phase 5.5 — extract the structured plan block when the model emits one.
    # The model is taught to put EITHER labeled prose OR a JSON object inside
    # `<PLAN>…</PLAN>`. We try JSON first; on parse failure we leave plan=None
    # and the extension renders the existing prose card unchanged.
    plan_struct = None
    try:
        _m_plan = re.search(r"<PLAN>\s*(\{[\s\S]*?\})\s*</PLAN>", reply or "")
        if _m_plan:
            _p_obj = json.loads(_m_plan.group(1))
            if isinstance(_p_obj, dict) and ("domains" in _p_obj or "steps" in _p_obj):
                plan_struct = {
                    "domains": list(_p_obj.get("domains") or [])[:10],
                    "steps": list(_p_obj.get("steps") or [])[:20],
                    "irreversible": list(_p_obj.get("irreversible") or [])[:10],
                }
    except Exception:
        plan_struct = None

    # Phase 5.6 — fire the turn_answer_start hook once per /chat response,
    # right before the reply is handed back. Observers only (the hook never
    # blocks); used for transcript tees + metrics. Best-effort: if the hook
    # bus errors, the response still ships.
    try:
        import hooks as _hooks_module  # noqa: WPS433
        _hooks_module.fire("turn_answer_start", reply or "",
                           action={"turn_id": turn_id, "round_num": round_num, "mode": effective_mode})
    except Exception:
        pass

    return {
        "reply": reply or "",
        "route": route,
        "model": model,
        "latency_ms": int((time.time() - t0) * 1000),
        "actions": captured_actions,
        "blocked_actions": blocked_actions,
        "mode": effective_mode,
        "turn_id": turn_id,
        "turn_root": turn_root or turn_id,
        "round_num": round_num,
        "plan": plan_struct,  # Phase 5.5 — structured plan when emitted; null otherwise.
        "round_budget": round_budget,
        "round_remaining": round_remaining,
        "done": done,
        "terminal_reason": terminal_reason,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

def safe_filename(name):
    name = re.sub(r'[^\w\s-]', '', name).strip()
    return re.sub(r'\s+', '_', name)[:60]

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPTS, **kwargs)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        # Bare / would show a directory listing of cwd (privacy leak); send to Pupil.
        if self.path in ('/', ''):
            self.send_response(302)
            self.send_header('Location', '/pupil.html')
            self.end_headers()
            return

        # /sdcpp/* → reverse-proxy to local sd-server on 127.0.0.1:7860.
        # Keeps the image engine loopback-bound while letting Pupil reach it
        # from any host that can reach :8080 (LAN, Tailscale). Same-origin.
        if self.path.startswith('/sdcpp/'):
            return self._proxy_sdcpp()

        # /project_summary?name=X[&refresh=1] — precomputed project briefing.
        # Reads the project's PROJECTS.md block + recent sessions, asks local
        # Ollama for a 5-bullet summary, caches under
        # ~/.master_ai_briefings/<slug>.json (or per-profile equivalent).
        # Returned immediately from cache when fresh (<6h); regenerated when
        # stale or when ?refresh=1. This is the "AI pre-reads your stuff so
        # you don't wait at the door" mechanism.
        if self.path == '/pupil.webmanifest':
            try:
                body = json.dumps({
                    'name': 'Pupil - Master AI',
                    'short_name': 'Pupil',
                    'description': 'Master AI browser UI for iOS, Android, and desktop.',
                    'start_url': '/pupil.html',
                    'scope': '/',
                    'display': 'standalone',
                    'orientation': 'portrait',
                    'background_color': '#F0F6FF',
                    'theme_color': '#2266CC',
                    'icons': [
                        {'src': '/pupil-icon.svg', 'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any maskable'}
                    ],
                }).encode()
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', 'application/manifest+json')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)
            except _CLIENT_DISCONNECTS:
                return
            return

        if self.path == '/pupil-icon.svg':
            try:
                svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="112" fill="#2266CC"/>
<circle cx="256" cy="206" r="98" fill="#F0F6FF"/>
<path d="M138 374c20-70 69-108 118-108s98 38 118 108" fill="none" stroke="#F0F6FF" stroke-width="54" stroke-linecap="round"/>
<path d="M174 190h164" stroke="#042C53" stroke-width="34" stroke-linecap="round"/>
<circle cx="222" cy="218" r="15" fill="#042C53"/>
<circle cx="290" cy="218" r="15" fill="#042C53"/>
</svg>'''.encode()
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', 'image/svg+xml')
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.send_header('Content-Length', len(svg))
                self.end_headers()
                self.wfile.write(svg)
            except _CLIENT_DISCONNECTS:
                return
            return

        if self.path == '/pupil-sw.js':
            try:
                body = b"""const CACHE = 'pupil-shell-v1';
const ASSETS = ['/pupil.html', '/pupil.webmanifest', '/pupil-icon.svg'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin) return;
  event.respondWith(fetch(event.request).then(response => {
    const copy = response.clone();
    caches.open(CACHE).then(cache => cache.put(event.request, copy));
    return response;
  }).catch(() => caches.match(event.request).then(hit => hit || caches.match('/pupil.html'))));
});
"""
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', 'application/javascript')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)
            except _CLIENT_DISCONNECTS:
                return
            return

        if self.path.startswith('/project_summary'):
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                name = (qs.get('name', [''])[0] or '').strip()
                force = qs.get('refresh', ['0'])[0] == '1'
                if not name:
                    self._json({'error': 'missing name param'}, 400); return

                # Profile-aware cache dir
                active_profile = ''
                try: active_profile = open(os.path.expanduser('~/.master_ai_active_profile')).read().strip()
                except Exception: pass
                if active_profile and os.path.isdir(os.path.expanduser(f'~/.master_ai_profiles/{active_profile}')):
                    cache_dir = os.path.expanduser(f'~/.master_ai_profiles/{active_profile}/briefings')
                else:
                    cache_dir = os.path.expanduser('~/.master_ai_briefings')
                os.makedirs(cache_dir, exist_ok=True)

                slug = re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_').lower()
                cache_path = os.path.join(cache_dir, slug + '.json')

                # Return cached if fresh and not forced
                import time as _time
                if (not force) and os.path.exists(cache_path):
                    try:
                        cached = json.load(open(cache_path))
                        if _time.time() - cached.get('cached_at', 0) < 6 * 3600:
                            cached['source'] = 'cache'
                            self._json(cached); return
                    except Exception:
                        pass

                # Build context: PROJECTS.md block + last 3 session tails
                pfile = os.path.expanduser('~/scripts/PROJECTS.md')
                block = ''
                try:
                    in_p = False
                    for ln in open(pfile).read().splitlines():
                        if ln.strip() == f'### {name}':
                            in_p = True; continue
                        if in_p and (ln.startswith('### ') or ln.startswith('## ')):
                            break
                        if in_p: block += ln + '\n'
                except Exception:
                    block = '(no PROJECTS.md entry)'

                sessions_tail = ''
                sessions_dir = os.path.expanduser('~/scripts/sessions')
                if os.path.isdir(sessions_dir):
                    logs = sorted(
                        [f for f in os.listdir(sessions_dir) if f.endswith('.log')],
                        key=lambda f: os.path.getmtime(os.path.join(sessions_dir, f)),
                        reverse=True
                    )[:3]
                    for f in logs:
                        try:
                            tail = open(os.path.join(sessions_dir, f)).read()[-2500:]
                            sessions_tail += f'\n=== {f} ===\n{tail}\n'
                        except Exception:
                            pass

                # Ask local Ollama for a bulletin
                # Trim inputs — shorter prompt = faster cold start.
                # The 7B coder model loads quicker than the 14B master model
                # and is fine for a 5-bullet summarization.
                block_trim = block[:1500]
                tail_trim = sessions_tail[-2500:] if sessions_tail else ''
                prompt = f"""Project briefing for someone returning after a break.
Project: {name}
Board:
{block_trim}

Recent snippet:
{tail_trim}

Output EXACTLY 5 short bullets, each starting with "- ". No preamble. No closing.
- state in one line
- what's blocking in one line
- last thing done in one line
- next move in one line
- anything easy to forget in one line"""
                summary_text = ''
                try:
                    req = urllib.request.Request(
                        'http://localhost:11434/api/generate',
                        data=json.dumps({
                            'model': 'qwen2.5:3b',
                            'prompt': prompt,
                            'stream': False,
                            'keep_alive': 0,
                            'options': {'num_predict': 140, 'temperature': 0.3},
                        }).encode(),
                        headers={'Content-Type': 'application/json'},
                    )
                    with urllib.request.urlopen(req, timeout=45) as resp:
                        data = json.loads(resp.read().decode())
                        summary_text = data.get('response', '').strip()
                except Exception as _e:
                    summary_text = f'(briefing generation failed: {_e}) — fallback to project board below.'

                payload = {
                    'name': name,
                    'summary': summary_text,
                    'project_block': block,
                    'cached_at': int(_time.time()),
                    'source': 'fresh',
                }
                # Only cache SUCCESSFUL briefings so a cold-Ollama timeout on
                # first try doesn't stick. Next request retries.
                if not summary_text.startswith('(briefing generation failed'):
                    with open(cache_path, 'w') as f:
                        json.dump(payload, f, indent=2)
                self._json(payload); return
            except Exception as e:
                self._error(str(e)); return

        # /sys — quick RAM + swap + loaded-model snapshot for the Pupil
        # sidebar. Cheap to compute; cheap to poll every 5s. Lets the user
        # see pressure before they open a third tab.
        if self.path == '/sys':
            try:
                mem = {'total_mb': 0, 'used_mb': 0, 'available_mb': 0, 'swap_used_mb': 0}
                try:
                    with open('/proc/meminfo') as f:
                        m = {}
                        for line in f:
                            k, _, v = line.partition(':')
                            v = v.strip().split()[0]
                            m[k] = int(v) // 1024   # KB → MB
                    mem['total_mb']     = m.get('MemTotal', 0)
                    mem['available_mb'] = m.get('MemAvailable', 0)
                    mem['used_mb']      = mem['total_mb'] - mem['available_mb']
                    mem['swap_used_mb'] = m.get('SwapTotal', 0) - m.get('SwapFree', 0)
                except Exception:
                    pass
                # Ask Ollama what's currently loaded (returns fast even cold)
                loaded = []
                try:
                    req = urllib.request.Request('http://localhost:11434/api/ps')
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        d = json.loads(resp.read().decode())
                        for m in d.get('models', []):
                            loaded.append({
                                'name': m.get('name'),
                                'size_mb': int(m.get('size_vram', m.get('size', 0))) // (1024*1024),
                            })
                except Exception:
                    pass
                self._json({'mem': mem, 'loaded_models': loaded})
            except Exception as e:
                self._error(str(e))
            return

        # Pupil API v1 — see ~/scripts/pupil_api.md.
        # /health — cheap liveness + Ollama reachability.
        if self.path == '/health':
            self._json(_health_payload())
            return

        # /status — richer state for the Pupil status card.
        if self.path == '/status':
            try:
                mode = 'plan'
                try:
                    mp = os.path.expanduser('~/.master_ai_mode')
                    if os.path.exists(mp):
                        v = open(mp).read().strip().lower()
                        if v in ('plan', 'review', 'auto'):
                            mode = v
                except Exception:
                    pass
                memory_facts = 0
                try:
                    mem_path = os.path.expanduser('~/.master_ai_memory')
                    if os.path.exists(mem_path):
                        with open(mem_path) as f:
                            memory_facts = sum(1 for _ in f)
                except Exception:
                    pass
                last_route = ''
                try:
                    rm = os.path.expanduser('~/.master_ai_router_metrics.jsonl')
                    if os.path.exists(rm):
                        with open(rm) as f:
                            last_line = ''
                            for line in f:
                                if line.strip():
                                    last_line = line
                            if last_line:
                                last_route = (json.loads(last_line).get('route') or '')
                except Exception:
                    pass
                # Reuse /sys logic for mem + loaded models
                mem = {'total_mb': 0, 'used_mb': 0, 'available_mb': 0, 'swap_used_mb': 0}
                try:
                    with open('/proc/meminfo') as f:
                        m = {}
                        for line in f:
                            k, _, v = line.partition(':')
                            v = v.strip().split()[0]
                            m[k] = int(v) // 1024
                    mem['total_mb']     = m.get('MemTotal', 0)
                    mem['available_mb'] = m.get('MemAvailable', 0)
                    mem['used_mb']      = mem['total_mb'] - mem['available_mb']
                    mem['swap_used_mb'] = m.get('SwapTotal', 0) - m.get('SwapFree', 0)
                except Exception:
                    pass
                loaded = []
                try:
                    req = urllib.request.Request('http://127.0.0.1:11434/api/ps')
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        d = json.loads(resp.read().decode())
                        for m in d.get('models', []):
                            loaded.append({
                                'name': m.get('name'),
                                'size_mb': int(m.get('size_vram', m.get('size', 0))) // (1024*1024),
                            })
                except Exception:
                    pass
                self._json({
                    'mode': mode,
                    'model': 'master-ai',
                    'memory_facts': memory_facts,
                    'last_route': last_route,
                    'queue_depth': 0,
                    'loaded_models': loaded,
                    'mem': mem,
                    'ts': datetime.now().astimezone().isoformat(timespec='seconds'),
                })
            except Exception as e:
                self._error(str(e))
            return

        # P1.7 /metrics — observability rollup over router metrics +
        # typed audit. Same summary the Sensei `stats` command renders;
        # Pupil can poll this to populate its stats panel.
        if self.path == '/metrics':
            try:
                import sys as _sys
                _scripts_dir = os.path.expanduser('~/scripts')
                if _scripts_dir not in _sys.path:
                    _sys.path.insert(0, _scripts_dir)
                import observability as _obs
                summary = _obs.summarize(limit=500)
                self._json(summary)
            except Exception as e:
                self._error(str(e))
            return

        # /events — SSE stream. P0.1 ships hello + heartbeat only.
        # Typed-action events (P0.4) and mode_changed (P1.4 wiring) come later.
        if self.path == '/events':
            try:
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'keep-alive')
                self.end_headers()
                import time as _time
                def _write_event(name, payload):
                    msg = f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()
                    self.wfile.write(msg)
                    self.wfile.flush()
                _write_event('hello', {'ts': datetime.now().astimezone().isoformat(timespec='seconds')})
                # Bounded loop — exits on client disconnect (BrokenPipeError).
                # 15s heartbeat; max 1 hour per connection so a stuck client doesn't pin a thread.
                end = _time.time() + 3600
                while _time.time() < end:
                    _time.sleep(15)
                    _write_event('heartbeat', {'ts': datetime.now().astimezone().isoformat(timespec='seconds')})
            except _CLIENT_DISCONNECTS:
                pass
            except Exception:
                pass
            return

        # /thoughts — canonical Master AI voice (trademark quotes + tips +
        # thinking phrases). Shared by Sensei and Pupil so both UIs speak in
        # one accord. Source of truth: ~/scripts/master_ai_voice.json.
        if self.path == '/thoughts':
            try:
                vpath = os.path.expanduser('~/scripts/master_ai_voice.json')
                if os.path.exists(vpath):
                    data = json.load(open(vpath))
                else:
                    data = {}
                self._json(data); return
            except Exception as e:
                self._error(str(e)); return

        # /node_info — public info this node announces to mesh peers.
        # Returned from any /node_info GET (localhost or Tailscale).
        # Scaffolding only: no auth yet, no routing yet. Enough for a
        # peer to confirm "yes there's a Master AI at this IP".
        if self.path == '/node_info':
            try:
                import socket as _sk, platform as _pl
                mesh_path = os.path.expanduser('~/.master_ai_mesh.json')
                mesh_cfg = {}
                try:
                    if os.path.exists(mesh_path):
                        mesh_cfg = json.load(open(mesh_path))
                except Exception:
                    mesh_cfg = {}
                active_model = ''
                try:
                    am = os.path.expanduser('~/.master_ai_active_model')
                    if os.path.exists(am):
                        active_model = open(am).read().strip()
                except Exception:
                    pass
                info = {
                    'node_name': mesh_cfg.get('node_name') or _sk.gethostname(),
                    'version': 'master-ai v1.8-testing',
                    'platform': _pl.system().lower(),
                    'active_model': active_model or 'auto',
                    'profile': _active_profile(),
                    'ports': {'stt': 8080, 'ollama': 11434, 'tts': 5050},
                }
                self._json(info); return
            except Exception as e:
                self._error(str(e)); return

        # /peers — list of peer nodes this node knows about, read from
        # ~/.master_ai_mesh.json. No live ping here — that's a separate
        # client-side job. This just exposes the address book.
        if self.path == '/peers':
            try:
                mesh_path = os.path.expanduser('~/.master_ai_mesh.json')
                peers = []
                if os.path.exists(mesh_path):
                    cfg = json.load(open(mesh_path))
                    peers = cfg.get('peers', []) or []
                self._json({'peers': peers}); return
            except Exception as e:
                self._error(str(e)); return

        # /profile — return the active Master AI profile name (empty string if
        # none). Pupil uses this to namespace its localStorage so each user
        # gets their own settings/sessions in the browser.
        if self.path == '/profile':
            try:
                p = os.path.expanduser('~/.master_ai_active_profile')
                name = open(p).read().strip() if os.path.exists(p) else ''
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'profile': name}).encode())
            except Exception as e:
                self._error(str(e))
            return

        # /keys — bridge between menu 11's ~/.master_ai_keys and Pupil's
        # localStorage-based wizard. Only exposed on localhost; the underlying
        # file is already chmod 600 owned by the user. Returns the JSON as-is.
        # Pupil uses this so you don't have to paste the same key twice.
        if self.path == '/keys':
            try:
                keys_file = os.path.expanduser('~/.master_ai_keys')
                if os.path.exists(keys_file):
                    with open(keys_file) as f:
                        data = json.load(f)
                else:
                    data = {}
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            except Exception as e:
                self._error(str(e))
            return

        if self.path == '/sessions':
            try:
                chats = _chats_dir()
                all_files = os.listdir(chats)
                gz_files  = [f for f in all_files if f.endswith('.json.gz')]
                json_files = [f for f in all_files if f.endswith('.json') and not f.endswith('.gz')]
                files = sorted(
                    gz_files + json_files,
                    key=lambda f: os.path.getmtime(os.path.join(chats, f)),
                    reverse=True
                )
                sessions = []
                for f in files:
                    try:
                        path = os.path.join(chats, f)
                        if f.endswith('.json.gz'):
                            with gzip.open(path, 'rt', encoding='utf-8') as fh:
                                data = json.load(fh)
                        else:
                            with open(path) as fh:
                                data = json.load(fh)
                        sessions.append({
                            'file': f,
                            'name': data.get('name', f),
                            'date': data.get('date', ''),
                            'ts':   data.get('ts', 0),
                            'source': data.get('source', 'Web UI'),
                            'messages': data.get('messages', [])
                        })
                    except Exception:
                        pass
                # Also load PC Control .log sessions from ~/scripts/sessions/
                pc_sessions_dir = os.path.join(SCRIPTS, 'sessions')
                if os.path.isdir(pc_sessions_dir):
                    for f in sorted(os.listdir(pc_sessions_dir), reverse=True):
                        if not f.endswith('.log'): continue
                        try:
                            path = os.path.join(pc_sessions_dir, f)
                            with open(path) as fh:
                                lines = fh.readlines()
                            msgs = []
                            for line in lines:
                                line = line.strip()
                                if line.startswith('[') and '] You: ' in line:
                                    msgs.append({'role':'user','content': line.split('] You: ',1)[1]})
                                elif line.startswith('[') and '] AI: ' in line:
                                    msgs.append({'role':'assistant','content': line.split('] AI: ',1)[1]})
                            if msgs:
                                ts = int(os.path.getmtime(path) * 1000)
                                sessions.append({
                                    'file': f,
                                    'name': f.replace('.log','').replace('_',' '),
                                    'date': datetime.fromtimestamp(ts/1000).strftime('%-m/%-d/%Y'),
                                    'ts': ts,
                                    'source': 'PC Control',
                                    'messages': msgs
                                })
                        except Exception:
                            pass
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(sessions).encode())
            except Exception as e:
                self._error(str(e))
        else:
            try:
                super().do_GET()
            except _CLIENT_DISCONNECTS:
                return

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        data   = self.rfile.read(length)

        # /sdcpp/* → reverse-proxy to local sd-server (see do_GET note).
        if self.path.startswith('/sdcpp/'):
            return self._proxy_sdcpp(body=data)

        # /extension/action_result — extension reports outcome of a typed
        # BROWSER_* dispatch. Audit-only sink (the extension already executed
        # the action in the tab per Branch B; backend never reaches DOM).
        # Body: {action_id, verdict: accept|reject|timeout, result: success|
        # failure|blocked, final_state?}. Requires X-Master-AI-Token when
        # called from a chrome-extension origin.
        if self.path == '/extension/action_result':
            if not self._require_extension_auth():
                return
            try:
                payload = json.loads(data or b'{}')
            except Exception:
                return self._json({'error': 'bad json'}, 400)
            try:
                from pathlib import Path as _P
                import time as _t
                rec = {
                    'ts': _t.strftime('%Y-%m-%dT%H:%M:%S'),
                    'source': 'extension',
                    'kind': 'extension_action_result',
                    'action_id': payload.get('action_id'),
                    'verdict':   payload.get('verdict'),
                    'result':    payload.get('result'),
                    'final_state': payload.get('final_state'),
                    # M9.2: irreversible-action heuristic label from side_panel.js
                    # classifyBrowserAction. None when the action wasn't gated.
                    # Stored alongside the verdict so analytics can answer
                    # "how often does the user approve gated purchases vs deletes?"
                    'gated_by': (
                        payload.get('gated_by') if isinstance(payload.get('gated_by'), str)
                        else (payload.get('action') or {}).get('gated_by')
                    ),
                    'raw': payload,
                }
                with _P.home().joinpath('.master_ai_audit_typed.jsonl').open('a') as f:
                    f.write(json.dumps(rec) + '\n')
            except Exception:
                pass  # Audit is observability, not a blocker.
            return self._json({'ok': True})

        # /extension/refusal_audit — extension reports a dispatch attempt
        # that was refused client-side BEFORE reaching the backend (e.g.,
        # heartbeat showed bridge unreachable, so sendPrompt() refused with
        # a structured "would have sent" message). The extension queues
        # these locally during the unreachable window and flushes them once
        # the bridge recovers, so the audit trail records honest-failure
        # attempts that would otherwise vanish without trace.
        #
        # Body: {correlation_id, ts (client), blocked_reason, prompt,
        # source, session_id, capabilities_fired: []}.
        # Requires X-Master-AI-Token. Phase 1 audit-discipline gap fix per
        # ~/.claude/plans/reactive-waddling-papert.md.
        if self.path == '/extension/refusal_audit':
            if not self._require_extension_auth():
                return
            try:
                payload = json.loads(data or b'{}')
            except Exception:
                return self._json({'error': 'bad json'}, 400)
            try:
                from pathlib import Path as _P
                import time as _t
                rec = {
                    'ts': _t.strftime('%Y-%m-%dT%H:%M:%S'),
                    'source': 'extension',
                    'kind': 'extension_refusal',
                    'correlation_id': payload.get('correlation_id'),
                    'client_ts': payload.get('ts'),
                    'blocked_reason': payload.get('blocked_reason') or 'bridge_unreachable',
                    'prompt': (payload.get('prompt') or '')[:2000],
                    'source_label': payload.get('source'),
                    'session_id': payload.get('session_id'),
                    'capabilities_fired': payload.get('capabilities_fired') or [],
                    'verification_results': [],
                    'raw': payload,
                }
                with _P.home().joinpath('.master_ai_audit_typed.jsonl').open('a') as f:
                    f.write(json.dumps(rec) + '\n')
            except Exception:
                pass  # Audit is observability, not a blocker.
            return self._json({'ok': True})

        # /extension/classify_domain — Phase 1.1 safety classifier. Tells the
        # extension whether an origin is safe to act on before any BROWSER_*
        # action touches a new host. Local-only — no live external lookup.
        # Backed by ~/.master_ai_domain_classes.json.
        #
        # Body: {domain} or {url}
        # Returns: {ok, category, reason, matched, host, ttl_s, source}
        if self.path == '/extension/classify_domain':
            if not self._require_extension_auth():
                return
            try:
                payload = json.loads(data or b'{}')
            except Exception:
                return self._json({'error': 'bad json'}, 400)
            target = payload.get('url') or payload.get('domain') or ''
            if not isinstance(target, str):
                return self._json({'error': 'domain/url must be a string'}, 400)
            result = _classify_domain(target)
            return self._json({'ok': True, **result})

        # /tool/find — Phase 7 semantic browser find. Body:
        #   {query, ax_tree}
        # Returns up to 20 {ref, name, role, selector, confidence} matches.
        # The implementation lives in subagents/find.py and is routed through
        # subagent_registry so the nested-search tool has one registry surface.
        if self.path == '/tool/find':
            if not self._require_extension_auth():
                return
            try:
                payload = json.loads(data or b'{}')
                return self._json(_tool_find(payload))
            except ValueError as e:
                return self._json({'ok': False, 'error': str(e)}, 400)
            except Exception as e:
                return self._json({'ok': False, 'error': str(e)}, 500)

        # /tool/describe_step — Phase 9.3 workflow-step description.
        # Body: {step, transcript?}. Returns {description}.
        if self.path == '/tool/describe_step':
            if not self._require_extension_auth():
                return
            try:
                payload = json.loads(data or b'{}')
                return self._json(_tool_describe_step(payload))
            except ValueError as e:
                return self._json({'ok': False, 'error': str(e)}, 400)
            except Exception as e:
                return self._json({'ok': False, 'error': str(e)}, 500)

        # /extension/resolve_local_file — deterministic local-document resolver
        # for user phrases like "my résumé" or "the AI query doc". It does not
        # read file contents; it returns bounded candidates so the model can
        # choose or ask when ambiguous.
        #
        # Body: {query, preferred_paths?}
        # Returns: {ok, query, ambiguous, candidates:[{path, score, reason, mtime, size}]}
        if self.path == '/extension/resolve_local_file':
            if not self._require_extension_auth():
                return
            try:
                payload = json.loads(data or b'{}')
            except Exception:
                return self._json({'error': 'bad json'}, 400)
            query = str(payload.get('query') or '').strip()
            preferred_paths = payload.get('preferred_paths') if isinstance(payload.get('preferred_paths'), list) else []
            try:
                _scripts_on_path()
                import master_ai as _m
                from pathlib import Path as _P
                import unicodedata as _ud

                def _norm(s):
                    return ''.join(ch for ch in _ud.normalize('NFD', str(s or '').lower())
                                   if _ud.category(ch) != 'Mn')

                qn = _norm(query)
                intent_terms = []
                if any(t in qn for t in ('resume', 'résumé', 'cv')):
                    intent_terms += ['resume', 'résumé', 'cv']
                if 'cover' in qn and 'letter' in qn:
                    intent_terms += ['cover', 'letter']
                if 'ai' in qn and 'query' in qn:
                    intent_terms += ['ai', 'query']
                if 'transcript' in qn:
                    intent_terms += ['transcript']
                if 'certificate' in qn or 'certification' in qn:
                    intent_terms += ['certificate', 'certification']
                if not intent_terms:
                    intent_terms = [w for w in re.split(r'[^a-z0-9]+', qn) if len(w) >= 3][:6]

                exts = {'.pdf', '.doc', '.docx', '.odt', '.rtf', '.txt', '.md', '.csv'}
                home = _P.home()
                roots = [
                    home / 'Desktop',
                    home / 'Documents',
                    home / 'Downloads',
                    home / 'Google Drive',
                    home / 'My Drive',
                    home,
                ]
                skip_dirs = {
                    '.cache', '.config', '.local', '.mozilla', '.npm', '.ollama',
                    '.var', '.vscode', '.codex', 'node_modules', '__pycache__',
                    'snap', '.git',
                }
                scored = {}

                def _read_ok(path):
                    if hasattr(_m, '_read_path_ok'):
                        try:
                            ok, _reason = _m._read_path_ok(str(path))
                            return bool(ok)
                        except Exception:
                            return False
                    return True

                def _score(path, preferred=False):
                    name = _norm(path.name)
                    full = _norm(str(path))
                    score = 1000 if preferred else 0
                    reasons = []
                    for term in intent_terms:
                        tn = _norm(term)
                        if not tn:
                            continue
                        if tn in name:
                            score += 120
                            reasons.append(f'name:{term}')
                        elif tn in full:
                            score += 40
                            reasons.append(f'path:{term}')
                    if path.suffix.lower() == '.pdf':
                        score += 25
                    try:
                        age_days = max(0.0, (time.time() - path.stat().st_mtime) / 86400.0)
                        score += max(0, int(30 - min(age_days, 30)))
                    except Exception:
                        pass
                    return score, ','.join(reasons[:5]) or ('preferred_path' if preferred else 'term_match')

                for raw in preferred_paths:
                    p = _P(os.path.expanduser(str(raw or ''))).resolve(strict=False)
                    if p.exists() and p.is_file() and p.suffix.lower() in exts and _read_ok(p):
                        score, reason = _score(p, preferred=True)
                        scored[str(p)] = (score, reason, p)

                seen_dirs = set()
                visited_files = 0
                for root in roots:
                    root = root.expanduser()
                    if not root.exists() or not root.is_dir():
                        continue
                    try:
                        root_real = str(root.resolve())
                    except Exception:
                        root_real = str(root)
                    if root_real in seen_dirs:
                        continue
                    seen_dirs.add(root_real)
                    for dirpath, dirnames, filenames in os.walk(root):
                        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith('.')]
                        depth = len(_P(dirpath).relative_to(root).parts) if _P(dirpath) != root else 0
                        if depth >= 5:
                            dirnames[:] = []
                        for filename in filenames:
                            visited_files += 1
                            if visited_files > 20000:
                                break
                            p = _P(dirpath) / filename
                            if p.suffix.lower() not in exts:
                                continue
                            score, reason = _score(p)
                            if score <= 0:
                                continue
                            try:
                                rp = p.resolve(strict=False)
                            except Exception:
                                rp = p
                            if not _read_ok(rp):
                                continue
                            key = str(rp)
                            if key not in scored or score > scored[key][0]:
                                scored[key] = (score, reason, rp)
                        if visited_files > 20000:
                            break

                rows = []
                for _key, (score, reason, p) in scored.items():
                    try:
                        st = p.stat()
                        rows.append({
                            'path': str(p),
                            'score': score,
                            'reason': reason,
                            'mtime': int(st.st_mtime),
                            'size': int(st.st_size),
                        })
                    except Exception:
                        rows.append({'path': str(p), 'score': score, 'reason': reason})
                rows.sort(key=lambda r: (-int(r.get('score') or 0), -int(r.get('mtime') or 0), r.get('path') or ''))
                top = rows[:8]
                ambiguous = len(top) > 1 and int(top[0].get('score') or 0) - int(top[1].get('score') or 0) < 40
                return self._json({'ok': True, 'query': query, 'ambiguous': ambiguous, 'candidates': top})
            except Exception as _e:
                return self._json({'ok': False, 'error': f'resolver failed: {_e}'}, 500)

        # /extension/read_local_file — reads a local file off disk for the
        # extension to ship into a content-script file-input via DataTransfer
        # (commit 2.1 of the dispatcher plan; full upload bridge needs a
        # content_script handler that lands in 2.1b after Codex's pile is
        # committed). Reuses master_ai._read_path_ok for the read fence;
        # 10MB cap to keep prompts bounded. Requires X-Master-AI-Token.
        #
        # Body: {path}
        # Returns: {ok, mime, size, base64}
        if self.path == '/extension/read_local_file':
            if not self._require_extension_auth():
                return
            try:
                payload = json.loads(data or b'{}')
            except Exception:
                return self._json({'error': 'bad json'}, 400)
            path_in = str(payload.get('path') or '').strip()
            if not path_in:
                return self._json({'ok': False, 'error': 'missing path'}, 400)
            try:
                _scripts_on_path()
                import master_ai as _m
            except Exception as _e:
                return self._json({'ok': False, 'error': f'master_ai import failed: {_e}'}, 500)
            expanded = os.path.expanduser(path_in)
            if hasattr(_m, '_read_path_ok'):
                try:
                    ok, reason = _m._read_path_ok(expanded)
                except Exception as _e:
                    ok, reason = False, f'read fence error: {_e}'
                if not ok:
                    return self._json({'ok': False, 'error': reason or 'read refused'}, 403)
            try:
                import mimetypes, base64
                if not os.path.exists(expanded):
                    return self._json({'ok': False, 'error': 'file not found'}, 404)
                size = os.path.getsize(expanded)
                if size > 10 * 1024 * 1024:
                    return self._json({'ok': False, 'error': f'file too large ({size} bytes; cap 10485760)'}, 413)
                mime, _ = mimetypes.guess_type(expanded)
                with open(expanded, 'rb') as f:
                    raw = f.read()
                b64 = base64.b64encode(raw).decode('ascii')
                # Audit (no base64 in the audit, just metadata).
                try:
                    from pathlib import Path as _P
                    import time as _t
                    rec = {
                        'ts': _t.strftime('%Y-%m-%dT%H:%M:%S'),
                        'source': 'extension',
                        'kind': 'extension_local_file_read',
                        'path': expanded[:500],
                        'size': size,
                        'mime': mime,
                    }
                    with _P.home().joinpath('.master_ai_audit_typed.jsonl').open('a') as f:
                        f.write(json.dumps(rec) + '\n')
                except Exception:
                    pass
                return self._json({'ok': True, 'path': os.path.abspath(expanded), 'size': size, 'mime': mime or 'application/octet-stream', 'base64': b64})
            except Exception as _e:
                return self._json({'ok': False, 'error': f'read failed: {_e}'}, 500)

        # /extension/approve_action — Review-mode per-action approval for
        # backend-dispatched directives (RUN/RUNTERM/READ/CREATE/EDIT).
        # Phase 1 commit 1.4 of the dispatcher plan. side_panel.js posts
        # this when the user clicks Allow on a non-BROWSER_* action card
        # in Review mode (or on a sensitivity-gated action in Auto mode).
        # Returns an ActionResult-shaped dict so side_panel can feed it
        # back into the M9 loop's action_results[] for the next round.
        #
        # Body: {action_id, action: {kind, target, content?, find?, replace?},
        #        verdict: 'accept'|'reject', mode_at_decision}.
        # Requires X-Master-AI-Token.
        if self.path == '/extension/approve_action':
            if not self._require_extension_auth():
                return
            try:
                payload = json.loads(data or b'{}')
            except Exception:
                return self._json({'error': 'bad json'}, 400)

            action = payload.get('action') if isinstance(payload.get('action'), dict) else {}
            verdict = str(payload.get('verdict') or 'accept').lower()
            mode_at = str(payload.get('mode_at_decision') or 'review').lower()
            kind = str(action.get('kind') or '').upper()
            target = str(action.get('target') or '')
            action_id = str(payload.get('action_id') or action.get('id') or '')

            # Reject path: short-circuit; no dispatch.
            if verdict in ('reject', 'decline'):
                envelope = {
                    'action_id': action_id, 'kind': kind, 'target': target,
                    'status': 'blocked', 'executed': False,
                    'error_code': 'user_declined',
                    'error_message': 'User declined the action in Review mode.',
                    'mode_at_emission': mode_at,
                }
                _audit_approve_action(action_id, action, verdict, envelope)
                return self._json({'ok': True, 'envelope': envelope})

            # BROWSER_* should be dispatched by side_panel.js directly, not
            # through this endpoint. If one arrives here it's a wire bug;
            # return a clear error envelope.
            if kind.startswith('BROWSER_'):
                envelope = {
                    'action_id': action_id, 'kind': kind, 'target': target,
                    'status': 'failure', 'executed': False,
                    'error_code': 'wrong_dispatcher',
                    'error_message': 'BROWSER_* actions dispatch via the content script, not /extension/approve_action.',
                    'mode_at_emission': mode_at,
                }
                _audit_approve_action(action_id, action, verdict, envelope)
                return self._json({'ok': False, 'envelope': envelope}, 400)

            # Dispatch one local action. Mirrors the chrome_extension auto-mode
            # block in api_handle; refactor into shared _dispatch_local_action
            # is a deferred follow-up to keep this commit focused.
            try:
                _scripts_on_path()
                import master_ai as _m
            except Exception as _e:
                envelope = {
                    'action_id': action_id, 'kind': kind, 'target': target,
                    'status': 'failure', 'executed': False,
                    'error_code': 'backend_unavailable',
                    'error_message': f'master_ai import failed: {_e}',
                    'mode_at_emission': mode_at,
                }
                _audit_approve_action(action_id, action, verdict, envelope)
                return self._json({'ok': False, 'envelope': envelope}, 500)

            # Force auto-mode semantics for this single dispatch — user has
            # already approved, so master_ai's interactive prompts should
            # bypass. Restore MODE afterward.
            prev_mode = getattr(_m, 'MODE', 'plan')
            _m.MODE = 'auto'
            status, executed = 'failure', False
            error_code = None
            error_message = None
            output_text = None
            try:
                if kind == 'RUN':
                    _r = _m.confirm_run(target)
                    if _r is None:
                        error_code, error_message = 'dispatcher_blocked', 'RUN refused by safeguard'
                    else:
                        executed = True
                        output_text = (getattr(_r, 'stdout', '') or '')[:2000]
                        if getattr(_r, 'ok', True):
                            status = 'success'
                        else:
                            status = 'failure'
                            error_code = 'nonzero_exit'
                            error_message = f'exit_code={getattr(_r, "exit_code", None)}'
                elif kind == 'RUNTERM':
                    _m.confirm_runterm(target)
                    executed = True
                    status = 'success'
                    output_text = f'[RUNTERM] dispatched: {target}'
                elif kind == 'READ':
                    expanded = os.path.expanduser(target)
                    if hasattr(_m, '_read_path_ok'):
                        ok, reason = _m._read_path_ok(expanded)
                        if not ok:
                            status, error_code, error_message = 'blocked', 'permission_required', reason
                        else:
                            try:
                                with open(expanded, 'r', errors='replace') as f:
                                    content = f.read()
                                executed = True
                                status = 'success'
                                output_text = content[:4000]
                            except Exception as e:
                                status = 'failure'
                                error_code = 'read_error'
                                error_message = str(e)[:240]
                elif kind == 'CREATE':
                    content_body = action.get('content') or action.get('body') or ''
                    ok = _m.confirm_create(target, content_body)
                    executed = bool(ok)
                    status = 'success' if ok else 'failure'
                    if not ok:
                        error_code = 'dispatcher_blocked'
                        error_message = 'CREATE refused (path fence or safeguard)'
                elif kind == 'EDIT':
                    find = action.get('find') or action.get('find_text') or ''
                    replace = action.get('replace') or action.get('replace_text') or ''
                    ok = _m.confirm_edit(target, find, replace)
                    executed = bool(ok)
                    status = 'success' if ok else 'failure'
                    if not ok:
                        error_code = 'dispatcher_blocked'
                        error_message = 'EDIT refused (path fence or safeguard)'
                else:
                    status = 'failure'
                    error_code = 'unsupported_kind'
                    error_message = f'kind {kind!r} not dispatched here'
            except Exception as e:
                status = 'failure'
                error_code = 'dispatch_exception'
                error_message = f'{type(e).__name__}: {e}'[:240]
            finally:
                try:
                    _m.MODE = prev_mode
                except Exception:
                    pass

            envelope = {
                'action_id': action_id, 'kind': kind, 'target': target[:300],
                'status': status, 'executed': executed,
                'error_code': error_code, 'error_message': error_message,
                'observed_text': output_text,
                'mode_at_emission': mode_at,
            }
            _audit_approve_action(action_id, action, verdict, envelope)
            return self._json({'ok': status == 'success', 'envelope': envelope})

        # /ask — federated routing endpoint. Accepts {prompt, model?} and runs
        # it through the LOCAL Ollama on this node, returning the response.
        # Auth: X-Mesh-Token header must match the token in ~/.master_ai_mesh.json.
        # No token configured → endpoint refuses all requests (fail-closed).
        # This is the mesh's "question on node A → answer from node B" pipe.
        if self.path == '/ask':
            try:
                mesh_path = os.path.expanduser('~/.master_ai_mesh.json')
                expected_token = ''
                if os.path.exists(mesh_path):
                    try:
                        expected_token = (json.load(open(mesh_path)).get('mesh_token') or '').strip()
                    except Exception:
                        expected_token = ''
                if not expected_token:
                    self._json({'error': 'mesh not configured (no mesh_token set in ~/.master_ai_mesh.json)'}, 503); return
                supplied = (self.headers.get('X-Mesh-Token') or '').strip()
                if supplied != expected_token:
                    self._json({'error': 'unauthorized (bad or missing X-Mesh-Token)'}, 401); return

                payload = json.loads(data or b'{}')
                prompt  = (payload.get('prompt') or '').strip()
                model   = (payload.get('model')  or 'qwen2.5:3b').strip()
                if not prompt:
                    self._json({'error': 'missing prompt'}, 400); return

                import urllib.request, time as _time
                t0 = _time.time()
                body = json.dumps({
                    'model': model,
                    'prompt': prompt,
                    'stream': False,
                    'options': {'num_predict': 512, 'temperature': 0.7}
                }).encode()
                req = urllib.request.Request('http://localhost:11434/api/generate',
                    data=body, headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=120) as r:
                    d = json.loads(r.read())
                self._json({
                    'response': d.get('response', ''),
                    'model': model,
                    'elapsed_s': round(_time.time() - t0, 2),
                    'node': os.uname().nodename if hasattr(os, 'uname') else 'unknown',
                }); return
            except Exception as e:
                self._json({'error': str(e)}, 500); return

        # Pupil API v1 — see ~/scripts/pupil_api.md.
        # POST /chat — Pupil's same-machine chat endpoint. No mesh-token gate.
        # M0a: non-interactive master_ai.handle() wrapper. It preserves the
        # v1 response shape while returning proposed actions for extension-side
        # confirmation; the backend never enters the TUI confirm_run path here.
        if self.path == '/chat':
            if not self._require_extension_auth():
                return
            try:
                payload = json.loads(data or b'{}')
                self._json(api_handle(payload)); return
            except ApiHandleBusy as e:
                # Wedge protection: lock acquire timed out. Surface fast so
                # the caller (Pupil / Chrome extension) can retry instead of
                # hanging behind a runaway local inference.
                self._json({
                    'error': 'system_busy',
                    'detail': str(e),
                    'retry_after_s': 15,
                }, 503); return
            except ValueError as e:
                msg = str(e)
                status = 400 if msg in ('missing prompt', 'invalid mode') else 500
                self._json({'error': msg}, status); return
            except Exception as e:
                self._json({'error': str(e)}, 500); return

        # POST /chat/continue — M9 Agentic Continuation Loop (2026-05-12).
        # The extension calls this AFTER dispatching the prior round's actions
        # and reporting their results to /extension/action_result. Body shape:
        #   {parent_turn_id, action_results:[{action_id,verdict,result,final_state,action}],
        #    prompt?, session_id, source}
        # If prompt is omitted, a synthetic "continue" is used so the model
        # reads only the [PREVIOUS ROUND RESULTS] block and the user goal in
        # history. Round budget caps runaway loops; the response includes
        # `done` so the extension knows when to stop auto-continuing.
        if self.path == '/chat/continue':
            if not self._require_extension_auth():
                return
            try:
                payload = json.loads(data or b'{}')
                if not (payload.get("prompt") or "").strip():
                    payload["prompt"] = "continue"
                if not payload.get("parent_turn_id"):
                    self._json({'error': 'missing parent_turn_id'}, 400); return
                self._json(api_handle(payload)); return
            except ApiHandleBusy as e:
                self._json({
                    'error': 'system_busy',
                    'detail': str(e),
                    'retry_after_s': 15,
                }, 503); return
            except ValueError as e:
                msg = str(e)
                status = 400 if msg in ('missing prompt', 'invalid mode', 'missing parent_turn_id') else 500
                self._json({'error': msg}, status); return
            except Exception as e:
                self._json({'error': str(e)}, 500); return

        # POST /mode — change current Sensei mode. Persists to ~/.master_ai_mode.
        if self.path == '/mode':
            try:
                payload = json.loads(data or b'{}')
                mode = (payload.get('mode') or '').strip().lower()
                if mode not in ('plan', 'review', 'auto'):
                    self._json({'error': 'invalid mode'}, 400); return
                mp = os.path.expanduser('~/.master_ai_mode')
                with open(mp, 'w') as f:
                    f.write(mode)
                self._json({'ok': True, 'mode': mode}); return
            except Exception as e:
                self._json({'error': str(e)}, 500); return

        # POST /voice — toggle Pupil TTS preference. Stored at ~/.master_ai_voice_enabled.
        if self.path == '/voice':
            try:
                payload = json.loads(data or b'{}')
                if 'enabled' not in payload or not isinstance(payload.get('enabled'), bool):
                    self._json({'error': "missing or invalid 'enabled' (must be boolean)"}, 400); return
                enabled = payload['enabled']
                engine = (payload.get('engine') or 'piper').strip() or 'piper'
                vp = os.path.expanduser('~/.master_ai_voice_enabled')
                with open(vp, 'w') as f:
                    json.dump({'enabled': enabled, 'engine': engine}, f)
                self._json({
                    'ok': True,
                    'voice_state': {'enabled': enabled, 'engine': engine},
                }); return
            except Exception as e:
                self._json({'error': str(e)}, 500); return

        # /fetch_url — Pupil's read-a-URL endpoint. Accepts {url} and
        # returns {url, markdown} by calling master_ai.firecrawl_fetch().
        # Needs a Firecrawl API key configured (see /keys). Different from
        # /web_search: that one returns snippets from many pages; this one
        # returns ONE page's full clean markdown content.
        if self.path == '/fetch_url':
            try:
                payload = json.loads(data or b'{}')
                url = (payload.get('url') or '').strip()
                if not url:
                    self._json({'error': 'missing url'}, 400); return
                import sys as _sys
                _here = os.path.dirname(os.path.abspath(__file__))
                if _here not in _sys.path:
                    _sys.path.insert(0, _here)
                import master_ai as _m
                markdown = _m.firecrawl_fetch(url)
                self._json({'url': url, 'markdown': markdown}); return
            except Exception as e:
                self._json({'error': str(e)}, 500); return

        # /web_search — live web search for Pupil (the browser UI). Pupil
        # detects time-sensitive questions client-side and POSTs here so
        # the same Gemini-grounded-then-DDG blend Sensei uses is available
        # in the browser. Returns {query, results, have_gemini} so Pupil
        # can display results and show the user which engine answered.
        # No auth — localhost / Tailscale only. No secrets returned.
        if self.path == '/web_search':
            try:
                payload = json.loads(data or b'{}')
                query = (payload.get('query') or '').strip()
                # engine: optional. "all" or omitted → full blender.
                # Anything else → single engine, header-wrapped so the
                # downstream detection dict still reports which answered.
                engine = (payload.get('engine') or '').strip().lower()
                # stream: when true AND running the blend, emit NDJSON
                # lines live — one per engine as it completes — so the
                # client can show results progressively instead of
                # waiting for the slowest engine (Gemini, ~20s).
                stream = bool(payload.get('stream'))
                if not query:
                    self._json({'error': 'missing query'}, 400); return
                # Import master_ai lazily — avoids a heavy import at server
                # startup and keeps stt_server functional even if master_ai
                # has a runtime issue.
                import sys as _sys
                _here = os.path.dirname(os.path.abspath(__file__))
                if _here not in _sys.path:
                    _sys.path.insert(0, _here)
                import master_ai as _m
                _engine_map = {
                    'gemini_grounded':    (_m.gemini_grounded_search, '[Google (via Gemini grounding)]'),
                    'brave':              (_m.brave_search,            '[Brave Search]'),
                    'serper':             (_m.serper_search,           '[Google (via Serper)]'),
                    'wikipedia':          (_m.wikipedia_search,        '[Wikipedia]'),
                    'duckduckgo':         (_m.duckduckgo_search,       '[DuckDuckGo]'),
                    'duckduckgo_instant': (_m.ddg_instant_answer,      '[DuckDuckGo Instant Answer]'),
                    'wikihow':            (_m.wikihow_via_gemini,      '[WikiHow (via Google site:)]'),
                }

                # ── Live-stream path ──
                # NDJSON over a Connection: close response. Each engine
                # emits start / done / empty / error. Client parses
                # line-by-line and renders as it reads. Fast engines
                # (Wikipedia ~1s) show up long before slow ones (Gemini).
                if stream and (not engine or engine == 'all'):
                    self.send_response(200)
                    self._cors()
                    self.send_header('Content-Type', 'application/x-ndjson')
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('X-Accel-Buffering', 'no')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    def _emit(obj):
                        try:
                            self.wfile.write((json.dumps(obj) + '\n').encode())
                            self.wfile.flush()
                        except Exception:
                            pass
                    _emit({'type': 'start', 'query': query,
                           'engines': list(_engine_map.keys())})
                    for key, (fn, header) in _engine_map.items():
                        _emit({'type': 'engine_start', 'engine': key})
                        try:
                            out = fn(query)
                        except Exception as e:
                            _emit({'type': 'engine_error', 'engine': key,
                                   'error': str(e)})
                            continue
                        if out:
                            _emit({'type': 'engine_done', 'engine': key,
                                   'header': header, 'result': out})
                        else:
                            _emit({'type': 'engine_empty', 'engine': key})
                    _emit({'type': 'done'})
                    return

                if engine and engine != 'all' and engine in _engine_map:
                    fn, header = _engine_map[engine]
                    out = fn(query)
                    results = f"{header}\n{out}" if out else f"Search unavailable: {engine} returned nothing."
                else:
                    results = _m.web_search(query)
                # Report which engines contributed so Pupil can show a
                # badge listing exactly what answered. Detection is by
                # the section headers master_ai.web_search() emits.
                r = results or ''
                engines = {
                    'gemini_grounded':     '[Google (via Gemini grounding)]' in r,
                    'brave':               '[Brave Search]' in r,
                    'serper':              '[Google (via Serper)]' in r,
                    'wikipedia':           '[Wikipedia]' in r,
                    'duckduckgo':          '[DuckDuckGo]' in r,
                    'duckduckgo_instant':  '[DuckDuckGo Instant Answer]' in r,
                    'wikihow':             '[WikiHow (via Google site:)]' in r,
                }
                self._json({
                    'query': query,
                    'results': results,
                    'engines': engines,
                }); return
            except Exception as e:
                self._json({'error': str(e)}, 500); return

        if self.path == '/stt':
            if not self._require_extension_auth():
                return
            tmp = tempfile.NamedTemporaryFile(suffix='.webm', delete=False)
            tmp.write(data)
            tmp.close()
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    import whisper
                    model = whisper.load_model('base')
                    result = model.transcribe(tmp.name)
                text = result['text'].strip()
                self._json({'text': text})
            except Exception as e:
                self._json({'error': str(e)}, 500)
            finally:
                try: os.unlink(tmp.name)
                except Exception: pass

        elif self.path == '/sessions':
            try:
                chats = _chats_dir()
                session = json.loads(data)
                name    = session.get('name', 'chat')
                ts      = session.get('ts', int(datetime.now().timestamp() * 1000))
                fname   = f"{ts}_{safe_filename(name)}.json.gz"
                with gzip.open(os.path.join(chats, fname), 'wt', encoding='utf-8') as f:
                    json.dump(session, f, separators=(',', ':'))
                # Auto-generate summary via local AI
                try:
                    import urllib.request
                    msgs = session.get('messages', [])
                    if len(msgs) >= 4:
                        transcript = "\n".join(
                            f"{m['role'].upper()}: {str(m.get('content',''))[:300]}"
                            for m in msgs[-30:] if m.get('role') in ('user','assistant')
                        )
                        prompt = ("Summarize this AI session in exactly 4 bullets. "
                                  "What was worked on, decided, unfinished, next steps. "
                                  "Format: • bullet\n\n" + transcript)
                        payload = json.dumps({'model':'qwen2.5:7b',
                                              'messages':[{'role':'user','content':prompt}],
                                              'stream':False}).encode()
                        req = urllib.request.Request('http://localhost:11434/api/chat',
                            data=payload, headers={'Content-Type':'application/json'})
                        with urllib.request.urlopen(req, timeout=30) as r:
                            summary = json.loads(r.read())['message']['content'].strip()
                        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                        summary_fname = fname.replace('.json.gz', '.summary')
                        summary_path = os.path.join(chats, summary_fname)
                        with open(summary_path, 'w') as sf:
                            sf.write(f"[Session {date_str}]\n{summary}\n")
                        # Append to memory (profile-aware)
                        prof = _active_profile()
                        if prof:
                            mem_file = os.path.expanduser(f"~/.master_ai_profiles/{prof}/memory")
                        else:
                            mem_file = os.path.expanduser("~/.master_ai_memory")
                        try:
                            existing = open(mem_file).read() if os.path.exists(mem_file) else ""
                            lines = [l for l in existing.splitlines() if not l.startswith("[Session ")]
                            session_lines = [l for l in existing.splitlines() if l.startswith("[Session ")][-4:]
                            with open(mem_file, 'w') as mf:
                                mf.write("\n".join(lines + session_lines +
                                    [f"[Session {date_str}]"] + summary.splitlines()) + "\n")
                        except Exception:
                            pass
                except Exception:
                    pass
                self._json({'saved': fname})
            except Exception as e:
                self._json({'error': str(e)}, 500)

        elif self.path == '/keys':
            # Merge incoming key(s) into ~/.master_ai_keys. Body shape:
            #   {"field": "groq", "value": "gsk_..."} — sets one
            #   OR {"merge": {"groq": "...", "openai": "..."}} — merges many
            # Duplicate protection: if a value for "field" exists and differs,
            # it becomes "{field}_2" instead of overwriting.
            try:
                payload   = json.loads(data)
                keys_file = os.path.expanduser('~/.master_ai_keys')
                try:
                    existing = json.load(open(keys_file)) if os.path.exists(keys_file) else {}
                except Exception:
                    existing = {}
                incoming = {}
                if isinstance(payload.get('merge'), dict):
                    incoming.update(payload['merge'])
                if payload.get('field') and payload.get('value') is not None:
                    incoming[payload['field']] = payload['value']
                for field, value in incoming.items():
                    if field in existing and existing[field] and existing[field] != value:
                        existing[field + '_2'] = value   # backup slot
                    else:
                        existing[field] = value
                with open(keys_file, 'w') as f:
                    json.dump(existing, f, indent=2)
                os.chmod(keys_file, 0o600)
                self._json({'saved': list(incoming.keys()), 'count': len(existing)})
            except Exception as e:
                self._json({'error': str(e)}, 500)

        elif self.path == '/sessions/delete':
            try:
                payload = json.loads(data)
                fname   = os.path.basename(payload.get('file', ''))
                chats   = _chats_dir()
                # support both .json and .json.gz
                path    = os.path.join(chats, fname)
                if not (fname and os.path.exists(path)):
                    alt = fname + '.gz' if not fname.endswith('.gz') else fname[:-3]
                    path = os.path.join(chats, alt)
                    fname = alt
                if fname and os.path.exists(path):
                    os.unlink(path)
                    self._json({'deleted': fname})
                else:
                    self._json({'error': 'not found'}, 404)
            except Exception as e:
                self._json({'error': str(e)}, 500)

        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        try:
            self.send_response(status)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECTS:
            return

    def _error(self, msg):
        self._json({'error': msg}, 500)

    def _cors(self):
        """Origin-aware CORS (M0a hardening 2026-05-12).
        - chrome-extension://... → echo Origin back; requests must carry X-Master-AI-Token.
        - Same-origin (no Origin header) → no Allow-Origin needed; browser allows.
        - Same-host (Pupil from LAN/Tailscale IPs matching request Host) → echo.
        - Anything else → no Allow-Origin → browser blocks.
        """
        origin = self.headers.get('Origin', '') or ''
        if origin.startswith('chrome-extension://'):
            self.send_header('Access-Control-Allow-Origin', origin)
        elif origin:
            host = self.headers.get('Host', '') or ''
            try:
                from urllib.parse import urlsplit
                o_netloc = urlsplit(origin).netloc
                if o_netloc and (o_netloc == host or o_netloc.split(':')[0] == host.split(':')[0]):
                    self.send_header('Access-Control-Allow-Origin', origin)
            except Exception:
                pass
        # else: no Origin header (same-origin) — nothing to echo.
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Master-AI-Token, X-Mesh-Token')

    def _extension_token(self):
        """Read the shared token from ~/.master_ai_extension_token. Cached on the class."""
        cls = self.__class__
        if not hasattr(cls, '_EXT_TOKEN'):
            try:
                from pathlib import Path
                cls._EXT_TOKEN = Path.home().joinpath('.master_ai_extension_token').read_text().strip() or None
            except Exception:
                cls._EXT_TOKEN = None
        return cls._EXT_TOKEN

    def _origin_is_extension(self):
        return (self.headers.get('Origin', '') or '').startswith('chrome-extension://')

    def _require_extension_auth(self):
        """If request comes from a chrome-extension origin, require a valid
        X-Master-AI-Token. Same-origin (Pupil) requests pass without a token.
        Returns True if the request should proceed; sends 401 and returns False otherwise."""
        if not self._origin_is_extension():
            return True  # Same-origin Pupil — legacy trust.
        sent = self.headers.get('X-Master-AI-Token', '') or ''
        expected = self._extension_token()
        if expected and sent and sent == expected:
            return True
        self.send_response(401)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        try:
            self.wfile.write(b'{"error":"extension token required"}')
        except Exception:
            pass
        return False

    def _proxy_sdcpp(self, body=b""):
        """Reverse-proxy /sdcpp/* to local sd-server on 127.0.0.1:7860.

        Keeps sd-server loopback-bound while letting Pupil reach it from any
        host that can reach :8080 (LAN, Tailscale). Same-origin → no CORS.
        Times: image gens take ~56s on this CPU, so timeout is generous.
        """
        upstream = "http://127.0.0.1:7860" + self.path
        req_headers = {}
        ct = self.headers.get("Content-Type")
        if ct:
            req_headers["Content-Type"] = ct
        try:
            req = urllib.request.Request(
                upstream, data=(body if body else None),
                headers=req_headers, method=self.command)
            with urllib.request.urlopen(req, timeout=180) as r:
                status = r.status
                up_ct = r.headers.get("Content-Type", "application/octet-stream")
                payload = r.read()
        except urllib.error.HTTPError as e:
            status = e.code
            up_ct = e.headers.get("Content-Type", "text/plain")
            payload = e.read() or str(e).encode()
        except Exception as e:
            status = 502
            up_ct = "application/json"
            payload = json.dumps({"error": f"sd-server unreachable: {e}"}).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", up_ct)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except _CLIENT_DISCONNECTS:
            pass

    def log_message(self, fmt, *args):
        pass

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    _prof = _active_profile()
    _prof_tag = f"  |  Profile: {_prof}" if _prof else ""
    print(f"🚀 Master AI server on :{port}  |  STT: Whisper  |  Sessions: {_chats_dir()}{_prof_tag}")
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
