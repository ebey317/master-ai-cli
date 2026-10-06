"""Model routing seam for Master AI.

Leaf module: no imports from master_ai, orchestration, dispatch, or context.
All routing logic, metrics, and helpers live here. Other modules import from this.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from typing import Any

from sensei_tables import (
    _ACKNOWLEDGMENT_RESPONSES,
    _IMAGE_PATH_RE,
    _VISION_INTENT_RE,
    _VISION_NEGATION_LOOKBACK,
    _VISION_NEGATION_RE,
    CLOUD_MODEL_NAMES,
    CODE_WORDS,
    COMPLEX_WORDS,
    OLLAMA_URL,
    REASONING_WORDS,
    ROUTER_METRICS_FILE,
    ROUTER_METRICS_MAX_SCAN,
    WEB_WORDS,
)

__all__ = [
    "MODELS",
    "PINNED_MODEL",
    "DEFAULT_LOCAL_MODEL",
    "_router_metric",
    "_router_recent_events",
    "_router_model_stats",
    "_router_perf_bonus",
    "_rank_route_candidates",
    "_choose_route",
    "format_router_stats",
    "_scrappy_model_present",
    "detect_route",
    "_route_from_fast_classifier",
    "_is_key_backed_model",
    "_is_explicit_vision_request",
    "_matches_terms",
    "_acknowledgment_short_circuit",
    "_classify_intent_fast",
    "_fast_classifier_enabled",
    "_json_object_from_text",
    "_CLOUD_CIRCUITS",
    "_NETWORK_DOWN_UNTIL",
    "_cloud_allowed",
    "_cloud_trip",
    "_cloud_trip_network",
]


def _fmt_ts(seconds: bool = False) -> str:
    """Lightweight timestamp for routing logs."""
    from datetime import datetime

    now = datetime.now()
    if seconds:
        return now.strftime("%H:%M:%S")
    return now.strftime("%H:%M")


def log(msg: Any) -> None:
    """Best-effort routing log write to the main log file."""
    try:
        from sensei_tables import LOG_FILE

        with open(LOG_FILE, "a") as f:
            f.write(f"[{_fmt_ts(seconds=True)}] {msg}\n")
    except Exception:
        pass


_CLOUD_CIRCUITS: dict[str, float] = {}
_NETWORK_DOWN_UNTIL = [0.0]  # mutable container for cross-module sharing
_SCRAPPY_CACHE = ""
_SCRAPPY_TS = 0.0


DEFAULT_LOCAL_MODEL = os.environ.get("MASTER_AI_LOCAL_MODEL", "") or None
if not DEFAULT_LOCAL_MODEL:
    try:
        import hardware_model as _hardware_model_early

        DEFAULT_LOCAL_MODEL = _hardware_model_early.pick_local_model()
    except Exception:
        DEFAULT_LOCAL_MODEL = "qwen2.5vl:3b"

MODELS = {
    "fast": DEFAULT_LOCAL_MODEL,
    "master": DEFAULT_LOCAL_MODEL,
    "vision": DEFAULT_LOCAL_MODEL,
    "coder": DEFAULT_LOCAL_MODEL,
    "general": DEFAULT_LOCAL_MODEL,
    "heavy": DEFAULT_LOCAL_MODEL,
    "qwen3": "qwen3.5:397b",
    "kimi": "kimi-k2.7-code",
}

PINNED_MODEL = None


def _is_key_backed_model(model: Any) -> bool:
    m = (model or "").lower()
    if (
        m.startswith("nvidia::")
        or m.startswith("cerebras::")
        or m.startswith("groq::")
        or m.startswith("qwen::")
    ):
        return True
    if "::" in m:
        return True
    return m in CLOUD_MODEL_NAMES or "/" in m


def _router_metric(kind: Any, **fields) -> None:
    """Append a compact router/feedback event. Best-effort only."""
    try:
        entry = {"ts": int(time.time()), "kind": kind}
        entry.update(fields)
        with ROUTER_METRICS_FILE.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _router_recent_events(limit: Any = ROUTER_METRICS_MAX_SCAN) -> list:
    try:
        if not ROUTER_METRICS_FILE.exists():
            return []
        lines = ROUTER_METRICS_FILE.read_text(errors="replace").splitlines()
    except Exception:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _router_model_stats(model: Any, task_type: Any | None = None) -> dict:
    def scan(match_task: Any) -> tuple:
        calls = failures = 0
        total_latency = 0.0
        for e in _router_recent_events():
            if e.get("kind") != "model_call" or e.get("model") != model:
                continue
            if match_task and e.get("task_type") not in (match_task, "fallback"):
                continue
            calls += 1
            if not e.get("ok"):
                failures += 1
            total_latency += float(e.get("latency_s") or 0.0)
        return calls, failures, total_latency

    calls, failures, total_latency = scan(task_type)
    if task_type and not calls:
        calls, failures, total_latency = scan(None)
    if not calls:
        return {"calls": 0, "success_rate": None, "avg_latency_s": None}
    return {
        "calls": calls,
        "success_rate": (calls - failures) / calls,
        "avg_latency_s": total_latency / calls,
    }


def _router_perf_bonus(model: Any, task_type: Any) -> float:
    """Small score adjustment from observed outcomes."""
    stats = _router_model_stats(model, task_type=task_type)
    if not stats["calls"]:
        return 0.0
    rate = stats["success_rate"]
    latency = stats["avg_latency_s"] or 0.0
    bonus = (rate - 0.80) * 20.0
    if stats["calls"] >= 3 and rate < 0.50:
        bonus -= 12
    if latency > 300:
        bonus -= 20
    elif latency > 180:
        bonus -= 14
    elif latency > 90:
        bonus -= 8
    elif latency and latency < 8:
        bonus += 3
    return max(-45.0, min(15.0, bonus))


def _rank_route_candidates(candidates: Any) -> list:
    ranked = []
    for cand in candidates:
        c = dict(cand)
        c["perf_bonus"] = round(
            _router_perf_bonus(c.get("model", ""), c.get("task_type", "")), 2
        )
        c["score"] = round(float(c.get("base_score", 0)) + c["perf_bonus"], 2)
        ranked.append(c)
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked


def _choose_route(candidates: Any, reason_prefix: str = "scored") -> dict:
    ranked = _rank_route_candidates(candidates)
    picked = ranked[0]
    decision = {k: picked[k] for k in ("route", "model") if k in picked}
    decision["reason"] = (
        f"{reason_prefix} → {picked.get('reason', picked.get('model'))} score={picked['score']:.1f}"
    )
    decision["score"] = picked["score"]
    decision["candidates"] = [
        {
            "route": c.get("route"),
            "model": c.get("model"),
            "score": c.get("score"),
            "reason": c.get("reason"),
        }
        for c in ranked
    ]
    return decision


def format_router_stats() -> str:
    events = _router_recent_events(limit=ROUTER_METRICS_MAX_SCAN)
    model_rows = {}
    exec_ok = exec_total = 0
    decisions = 0
    for e in events:
        if e.get("kind") == "route_decision":
            decisions += 1
        elif e.get("kind") == "execution":
            exec_total += 1
            exec_ok += 1 if e.get("ok") else 0
        elif e.get("kind") == "model_call":
            key = e.get("model") or "?"
            row = model_rows.setdefault(key, {"calls": 0, "ok": 0, "lat": 0.0})
            row["calls"] += 1
            row["ok"] += 1 if e.get("ok") else 0
            row["lat"] += float(e.get("latency_s") or 0.0)
    lines = ["Router feedback"]
    lines.append(f"   file      : {ROUTER_METRICS_FILE}")
    lines.append(f"   decisions : {decisions}")
    if model_rows:
        parts = []
        for model, row in sorted(model_rows.items(), key=lambda x: -x[1]["calls"])[:8]:
            rate = (row["ok"] / row["calls"]) * 100 if row["calls"] else 0
            avg = row["lat"] / row["calls"] if row["calls"] else 0
            parts.append(f"{model}={row['ok']}/{row['calls']} ok, {avg:.1f}s avg")
        lines.append("   models    : " + " | ".join(parts))
    if exec_total:
        lines.append(f"   execution : {exec_ok}/{exec_total} ok")
    return "\n".join(lines)


def _scrappy_model_present() -> str:
    """Return Ollama tag of the first 'scrappy' model pulled, else ''.
    Cached for 60s so orchestrator calls don't thrash."""
    global _SCRAPPY_CACHE, _SCRAPPY_TS
    now = time.time()
    try:
        if (now - _SCRAPPY_TS) < 60:
            return _SCRAPPY_CACHE
    except Exception:
        pass
    tag = ""
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
            body = r.read().decode()
        m = re.search(
            r'"(name|model)"\s*:\s*"([^"]*scrappy[^"]*)"', body, re.IGNORECASE
        )
        if m:
            tag = m.group(2)
    except Exception:
        pass
    _SCRAPPY_CACHE = tag
    _SCRAPPY_TS = now
    return tag


def _matches_terms(text: Any, words: Any, terms: Any) -> bool:
    """True when a term set contains either exact words or phrases."""
    if not terms:
        return False
    single = {t for t in terms if " " not in t}
    phrase = [t for t in terms if " " in t]
    return bool(words & single) or any(p in text for p in phrase)


def _is_explicit_vision_request(text: str) -> bool:
    """Vision routes need an image-extension path or a verb+vision-noun phrase."""
    if _IMAGE_PATH_RE.search(text):
        return True
    m = _VISION_INTENT_RE.search(text)
    if m:
        start = m.start()
        window = text[max(0, start - _VISION_NEGATION_LOOKBACK) : start]
        if _VISION_NEGATION_RE.search(window):
            return False
        return True
    return False


def detect_route(text: str, has_image: bool = False) -> tuple:
    global PINNED_MODEL
    t = text.lower()
    words = set(re.sub(r"[^\w\s]", "", t).split())  # strip punctuation so "weather?" matches WEB_WORDS

    if PINNED_MODEL:
        if _is_key_backed_model(PINNED_MODEL):
            return "cloud", PINNED_MODEL, f"selected → {PINNED_MODEL}"
        return "local", PINNED_MODEL, f"selected → {PINNED_MODEL}"

    if has_image or _is_explicit_vision_request(text):
        return (
            "vision",
            MODELS["kimi"],
            "vision → kimi-k2.5 (1T) · llava locally in local mode",
        )
    if words & CODE_WORDS:
        return "local", MODELS["coder"], f"code → {MODELS['coder']}"
    if _matches_terms(t, words, WEB_WORDS):
        return "web", None, "web → Gemini + search"
    if _matches_terms(t, words, REASONING_WORDS):
        return "cloud", "deepseek-r1", "reasoning → DeepSeek R1"
    if _matches_terms(t, words, COMPLEX_WORDS):
        return "local", MODELS["qwen3"], "complex → qwen3.5:cloud (397B)"
    return "local", MODELS["master"], f"general → {MODELS['master']}"


def _acknowledgment_short_circuit(text: Any) -> str:
    low = (text or "").strip().lower()
    if not low or "\n" in low or ":" in low or "/" in low:
        return ""
    normalized = " ".join(re.findall(r"[a-z0-9']+", low))
    if not normalized or len(normalized.split()) > 2:
        return ""
    return _ACKNOWLEDGMENT_RESPONSES.get(normalized, "")


def _fast_classifier_enabled() -> bool:
    flag = os.environ.get("SENSEI_FAST_CLASSIFIER", "0").strip().lower()
    return flag not in {"0", "false", "off", "no"}


def _json_object_from_text(text: Any) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _classify_intent_fast(
    user_text: Any, *, model: Any | None = None, timeout_s: Any | None = None
) -> dict | None:
    """Use the installed 3B model as a cheap first-pass intent classifier."""
    text = str(user_text or "").strip()
    if not text or len(text) > 800 or not _fast_classifier_enabled():
        return None
    model = model or MODELS["fast"]
    timeout_s = float(
        timeout_s or os.environ.get("SENSEI_FAST_CLASSIFIER_TIMEOUT", "4")
    )
    system = (
        "Classify one user message for a local computer-control agent. "
        "Return ONLY compact JSON with keys: intent, confidence, normalized_prompt, reply. "
        "intent is one of: ack, directive, conversation. "
        "Use ack only for short acknowledgments like ok/thanks/roger/got it. "
        "Use directive only for requests about the local machine, files, ports, processes, "
        "or installed software. If directive, normalized_prompt MUST be one of these shapes: "
        '"what\'s on port N", "where is NAME", "find NAME", "list files in PATH", '
        '"open file PATH", "is NAME running", "is NAME installed". '
        "Never output shell commands."
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "keep_alive": "5m",
        "options": {"temperature": 0, "num_ctx": 512},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        log(f"FAST_CLASSIFIER_ERROR: {e}")
        _router_metric("fast_classifier", model=model, ok=False, error=str(e)[:160])
        return None
    content = (((result or {}).get("message") or {}).get("content") or "").strip()
    obj = _json_object_from_text(content)
    if not obj:
        _router_metric(
            "fast_classifier",
            model=model,
            ok=False,
            error="bad_json",
            latency_s=round(time.time() - t0, 3),
        )
        return None
    intent = str(obj.get("intent") or "").strip().lower()
    try:
        confidence = float(obj.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    out = {
        "intent": intent,
        "confidence": max(0.0, min(confidence, 1.0)),
        "normalized_prompt": str(obj.get("normalized_prompt") or "").strip(),
        "reply": str(obj.get("reply") or "").strip(),
        "model": model,
    }
    _router_metric(
        "fast_classifier",
        model=model,
        ok=True,
        intent=out["intent"],
        confidence=out["confidence"],
        latency_s=round(time.time() - t0, 3),
    )
    return out


def _route_from_fast_classifier(user_text: Any) -> dict | None:
    cls = _classify_intent_fast(user_text)
    if not isinstance(cls, dict):
        return None
    intent = str(cls.get("intent") or "").lower()
    try:
        confidence = float(cls.get("confidence") or 0.0)
    except Exception:
        return None
    if confidence < 0.78:
        return None
    if intent == "ack":
        reply = cls.get("reply") or _acknowledgment_short_circuit(user_text) or "Okay."
        return {
            "route": "acknowledgment",
            "response": reply,
            "model": cls.get("model") or MODELS["fast"],
            "reason": f"tier-one classifier ack confidence={confidence:.2f}",
        }
    if intent == "directive":
        normalized = cls.get("normalized_prompt") or user_text
        directive = _deterministic_intent_to_directive(
            normalized
        ) or _deterministic_intent_to_directive(user_text)
        if directive:
            return {
                "route": "deterministic_intent",
                "synth_reply": directive,
                "model": cls.get("model") or MODELS["fast"],
                "reason": f"tier-one classifier directive confidence={confidence:.2f}",
            }
    return None


def _deterministic_intent_to_directive(normalized: str) -> str | None:
    """Map normalized intent prompts to deterministic directive strings.
    Returns None if no match — caller should fall back to normal routing."""
    low = normalized.lower().strip()
    if low.startswith("what") and "port" in low:
        m = re.search(r"port\s+(\d+)", low)
        if m:
            return f"RUNTERM: lsof -i :{m.group(1)}"
    if low.startswith("where is") or low.startswith("find "):
        target = low.replace("where is", "").replace("find", "").strip()
        if target:
            return f"RUNTERM: find / -name '*{target}*' 2>/dev/null | head -20"
    if low.startswith("list files in") or low.startswith("ls "):
        path = low.replace("list files in", "").replace("ls", "").strip() or "."
        return f"RUNTERM: ls -la {path}"
    if low.startswith("open file "):
        path = low.replace("open file", "").strip()
        if path:
            return f"READ: {path}"
    if low.startswith("is ") and ("running" in low or "installed" in low):
        name = (
            low.replace("is", "")
            .replace("running", "")
            .replace("installed", "")
            .strip()
        )
        if name:
            return f"RUNTERM: which {name} || dpkg -l | grep {name} || rpm -q {name}"
    return None


def _cloud_allowed(provider: Any) -> bool:
    now = time.time()
    if now < _NETWORK_DOWN_UNTIL[0]:
        log(f"CLOUD_SKIP [{provider}]: network circuit open")
        return False
    until = _CLOUD_CIRCUITS.get(provider, 0)
    if until and now < until:
        log(f"CLOUD_SKIP [{provider}]: provider circuit open")
        return False
    return True


def _cloud_trip(provider: Any, reason: Any, seconds: int = 30) -> None:
    _CLOUD_CIRCUITS[provider] = time.time() + seconds
    log(f"CLOUD_CIRCUIT [{provider}]: {reason} for {seconds}s")


def _cloud_trip_network(reason: Any, seconds: int = 60) -> None:
    """Trip the global network circuit for genuine connectivity failures.
    Plain timeouts are treated as provider-local and do NOT trip the shared circuit.
    """
    if "timed out" in str(reason).lower():
        log(
            f"CLOUD_NETWORK_SKIP: timeout treated as provider-local, not tripping the shared circuit ({reason})"
        )
        return
    _NETWORK_DOWN_UNTIL[0] = time.time() + seconds
    log(f"CLOUD_NETWORK_DOWN: {reason} for {seconds}s")
