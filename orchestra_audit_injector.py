#!/usr/bin/env python3
"""
orchestra_audit_injector.py — Drop-in audit logger for CLAF

Add this to your claf_config.py or run it after importing claf_config:
    import orchestra_audit_injector
    orchestra_audit_injector.install()

This wraps _select_mode, _is_hard_task, _pick_cloud_peer, and the main
chat/complete function to log every routing decision to /tmp/orchestra_audit.log
"""

import json
import os
import time
from datetime import datetime

AUDIT_LOG = os.environ.get("ORCHESTRA_AUDIT_LOG", "/tmp/orchestra_audit.log")

# Cost map per 1M tokens
COST_MAP = {
    "qwen2.5:3b": {"input": 0.0, "output": 0.0, "provider": "local-ollama"},
    "qwen2.5:7b": {"input": 0.0, "output": 0.0, "provider": "local-ollama"},
    "fast-agent:latest": {"input": 0.0, "output": 0.0, "provider": "local-ollama"},
    "qwen3-coder:480b-cloud": {
        "input": 0.0,
        "output": 0.0,
        "provider": "ollama-cloud-coder",
    },
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79, "provider": "groq"},
    "qwen-3-235b-a22b-instruct-2507": {
        "input": 0.50,
        "output": 0.50,
        "provider": "cerebras",
    },
    "accounts/fireworks/models/deepseek-v4-pro": {
        "input": 3.00,
        "output": 15.00,
        "provider": "fireworks",
    },
    "anthropic/claude-sonnet-4.6": {
        "input": 3.00,
        "output": 15.00,
        "provider": "openrouter",
    },
    "claude-haiku-4-5-20251001": {
        "input": 1.00,
        "output": 5.00,
        "provider": "anthropic",
    },
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60, "provider": "gemini"},
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    rates = COST_MAP.get(model, {"input": 0.0, "output": 0.0, "provider": "unknown"})
    return round(
        (in_tok * rates["input"] / 1_000_000) + (out_tok * rates["output"] / 1_000_000),
        6,
    )


def _write(entry: dict):
    with open(AUDIT_LOG, "a", buffering=1) as f:
        f.write(json.dumps(entry) + "\n")


# ── State holders (module-level, thread-safe enough for single-process CLAF) ──

_last_select_mode = {}
_last_hard_check = {}
_last_cloud_peer = {}


def install():
    """Monkey-patch CLAF config module. Call once at startup."""
    try:
        import claf_config as cc
    except ImportError:
        print("[orchestra_audit] ERROR: claf_config not found — cannot patch")
        return

    # Wrap _select_mode
    if hasattr(cc, "_select_mode"):
        orig = cc._select_mode

        def _wrapped_select_mode(body, *args, **kwargs):
            t0 = time.time()
            result = orig(body, *args, **kwargs)
            latency = (time.time() - t0) * 1000

            # Extract prompt length
            prompt = str(body.get("prompt", body.get("messages", "")))
            payload_len = len(prompt)

            _last_select_mode.clear()
            _last_select_mode.update(
                {
                    "result": result[0] if isinstance(result, tuple) else result,
                    "scores": result[1]
                    if isinstance(result, tuple) and len(result) > 1
                    else {},
                    "latency_ms": round(latency, 1),
                    "prompt_snippet": prompt[:120],
                    "payload_len": payload_len,
                }
            )
            return result

        cc._select_mode = _wrapped_select_mode
        print("[orchestra_audit] Patched _select_mode")
    else:
        print("[orchestra_audit] WARNING: _select_mode not found")

    # Wrap _is_hard_task
    if hasattr(cc, "_is_hard_task"):
        orig = cc._is_hard_task

        def _wrapped_is_hard_task(metadata, messages, *args, **kwargs):
            t0 = time.time()
            result = orig(metadata, messages, *args, **kwargs)
            latency = (time.time() - t0) * 1000

            reason = None
            if result:
                if getattr(metadata, "escalate", False):
                    reason = "metadata.escalate=True"
                elif len(str(getattr(metadata, "system_prompt", ""))) > 40000:
                    reason = "system_prompt>40k_chars"
                elif len(messages) > 60:
                    reason = "message_count>60"
                elif messages and "[ESCALATE]" in str(
                    messages[-1].get("content", "")
                    if isinstance(messages[-1], dict)
                    else ""
                ):
                    reason = "[ESCALATE]_tag"
                else:
                    reason = "unknown"

            _last_hard_check.clear()
            _last_hard_check.update(
                {
                    "result": result,
                    "reason": reason,
                    "latency_ms": round(latency, 1),
                }
            )
            return result

        cc._is_hard_task = _wrapped_is_hard_task
        print("[orchestra_audit] Patched _is_hard_task")
    else:
        print("[orchestra_audit] WARNING: _is_hard_task not found")

    # Wrap _pick_cloud_peer
    if hasattr(cc, "_pick_cloud_peer"):
        orig = cc._pick_cloud_peer

        def _wrapped_pick(*args, **kwargs):
            t0 = time.time()
            result = orig(*args, **kwargs)
            latency = (time.time() - t0) * 1000

            _last_cloud_peer.clear()
            _last_cloud_peer.update(
                {
                    "tier": result,
                    "latency_ms": round(latency, 1),
                }
            )
            return result

        cc._pick_cloud_peer = _wrapped_pick
        print("[orchestra_audit] Patched _pick_cloud_peer")
    else:
        print("[orchestra_audit] WARNING: _pick_cloud_peer not found")

    # Wrap the main dispatch/chat function
    dispatch_name = None
    for fname in ["chat", "complete", "generate", "dispatch", "infer"]:
        if hasattr(cc, fname):
            dispatch_name = fname
            break

    if dispatch_name:
        orig_fn = getattr(cc, dispatch_name)

        def _wrapped_dispatch(*args, **kwargs):
            t0 = time.time()

            # Try to extract body from args
            body = kwargs.get("body", kwargs)
            if args and isinstance(args[0], dict):
                body = args[0]

            try:
                resp = orig_fn(*args, **kwargs)
                error = None

                # Try to extract token usage from response
                in_tok = 0
                out_tok = 0
                if isinstance(resp, dict):
                    usage = resp.get("usage", {})
                    in_tok = usage.get("input_tokens", 0) or usage.get(
                        "prompt_tokens", 0
                    )
                    out_tok = usage.get("output_tokens", 0) or usage.get(
                        "completion_tokens", 0
                    )

            except Exception as e:
                error = str(e)
                resp = None
                in_tok = 0
                out_tok = 0
                raise
            finally:
                latency = (time.time() - t0) * 1000

                # Build the audit entry
                mode_info = _last_select_mode.copy()
                hard_info = _last_hard_check.copy()
                peer_info = _last_cloud_peer.copy()

                # Resolve model from config
                cfg = getattr(cc, "config", {})
                providers = cfg.get("providers", [])
                tier = peer_info.get("tier", -1)
                model = "unknown"
                provider = "unknown"
                for p in providers:
                    if p.get("tier") == tier:
                        model = p.get("model", "unknown")
                        provider = p.get("name", "unknown")
                        break

                # Fallback: if no tier match, try to infer from mode
                if model == "unknown" and mode_info.get("result") == "local":
                    model = cfg.get("local_provider", "qwen2.5:7b")
                    provider = "local-ollama"

                # Estimate tokens if not in response
                if in_tok == 0:
                    prompt = str(body.get("prompt", body.get("messages", "")))
                    in_tok = len(prompt) // 4
                if out_tok == 0:
                    out_tok = 500  # rough estimate

                cost = _estimate_cost(model, in_tok, out_tok)

                entry = {
                    "timestamp": _now(),
                    "task_type": body.get("task_type", dispatch_name),
                    "prompt_snippet": mode_info.get(
                        "prompt_snippet", str(body.get("prompt", ""))[:120]
                    ),
                    "mode": mode_info.get("result", "unknown"),
                    "payload_len": mode_info.get("payload_len", 0),
                    "is_hard_task": hard_info.get("result", False),
                    "hard_reason": hard_info.get("reason"),
                    "tier": tier,
                    "provider": provider,
                    "model": model,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "latency_ms": round(latency, 1),
                    "cost_usd": cost,
                    "error": error,
                }
                _write(entry)

            return resp

        setattr(cc, dispatch_name, _wrapped_dispatch)
        print(f"[orchestra_audit] Patched {dispatch_name}")
    else:
        print("[orchestra_audit] WARNING: No dispatch/chat function found")

    print(f"[orchestra_audit] Install complete. Logging to {AUDIT_LOG}")


if __name__ == "__main__":
    install()
