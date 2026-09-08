"""capabilities.py — capability registry for Sensei's agentic loop.

Phase 1 of the Reactive Waddling Papert plan
(~/.claude/plans/reactive-waddling-papert.md).

The registry is the single source of truth for what the agent can do.
Both Modelfile and CLOUD_SYSTEM should eventually stamp REGISTRY_VERSION
into the prompt assembly so audit rows can correlate behavior to capability
state.

ONE capability registered in phase 1: desktop.launch_app for opening a
curated allowlist of desktop applications. Phase 2 adds terminal.run_command,
code.read_file, code.edit_file, browser.read_dom, and others.

Decision is the load-bearing shape — api_handle dispatches on the Decision
object, not on a raw boolean. For Hypnotix the decision is
{allow=True, requires_confirmation=False}; the SHAPE is already correct for
the next capability that needs confirmation (e.g.,
terminal.run_destructive_command).
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Callable, Optional

REGISTRY_VERSION = "0.1.0"
SAFETY_POLICY_VERSION = "0.1.0"


# --- Policy dataclasses ---------------------------------------------------


@dataclass
class RetryPolicy:
    max_retries: int = 0
    backoff_s: float = 1.0


@dataclass
class VerificationPolicy:
    """Where + how to verify a capability's effect.

    verifier_name is a dotted path resolved lazily (verifiers.verify_process_running).
    Lazy resolution keeps capabilities.py importable even if verifiers.py has
    not been loaded yet, and lets the executor module list capabilities
    statically without import cycles.
    """

    verifier_name: str
    max_wait_s: float = 5.0
    poll_ms: int = 500


@dataclass
class Capability:
    name: str
    input_schema: dict
    executor_name: str  # dotted path: e.g. "master_ai.confirm_run"
    permission_tier: str  # "allow" | "requires_confirmation" | "blocked"
    risk_tier: str  # "low" | "medium" | "high"
    cost_tier: str  # "cheap" | "moderate" | "expensive"
    verification_policy: VerificationPolicy
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    fallback_options: list = field(default_factory=list)
    failure_messages: dict = field(default_factory=dict)

    def resolve_executor(self) -> Callable:
        module_name, _, func_name = self.executor_name.rpartition(".")
        mod = importlib.import_module(module_name)
        return getattr(mod, func_name)

    def resolve_verifier(self) -> Callable:
        name = self.verification_policy.verifier_name
        module_name, _, func_name = name.rpartition(".")
        mod = importlib.import_module(module_name)
        return getattr(mod, func_name)


@dataclass
class Decision:
    """The result of Registry.lookup(). api_handle dispatches on this.

    capability is None when no specific capability matched — that signals the
    caller to fall through to the existing generic dispatch path. This keeps
    the registry opt-in during phase 1, so unregistered directives still
    work as they did before.

    verify_target carries capability-specific verification input (e.g., the
    app name for desktop.launch_app) so the api_handle code doesn't need to
    re-parse the directive args to invoke the verifier.
    """

    allow: bool
    requires_confirmation: bool
    reason: str
    capability: Optional[Capability] = None
    verify_target: Optional[str] = None


# --- Allowlists -----------------------------------------------------------


# Phase 1: a small curated set so the Hypnotix vertical slice is real
# without exposing arbitrary RUN bypasses. Phase 2 expands this and routes
# more apps through terminal.run_command with richer schemas.
DESKTOP_APP_ALLOWLIST = {"hypnotix", "thunderbird"}


def _matches_desktop_launch(cmd: str) -> Optional[str]:
    """Return the matched app name if cmd is a simple launch of an allowed app.

    Accepts `<app>` or `<app> &`. Rejects anything with extra arguments to
    keep the phase-1 surface narrow. Phase 2 swaps this for input-schema
    validation against the capability's input_schema field.
    """
    if not cmd:
        return None
    tokens = cmd.strip().split()
    if not tokens:
        return None
    first = tokens[0]
    if first in DESKTOP_APP_ALLOWLIST:
        rest = tokens[1:]
        if not rest or rest == ["&"]:
            return first
    return None


def _parse_notify_args(cmd: str) -> tuple[str, str, str]:
    """Best-effort parse of a notify-send or sensei-notify directive.

    notify-send 'title' 'body'       → title, body, 'normal'
    sensei-notify.sh title body      → title, body, 'normal'
    sensei-notify.sh title body crit → title, body, 'critical'
    """
    import shlex
    title = "Sensei"
    body = ""
    urgency = "normal"
    if not cmd:
        return title, body, urgency
    try:
        parts = shlex.split(cmd.strip())
    except Exception:
        parts = cmd.strip().split()
    if not parts:
        return title, body, urgency
    # Drop the executable name
    idx = 1
    if idx < len(parts) and parts[0] == "notify-send":
        if idx < len(parts):
            title = parts[idx]
            idx += 1
        if idx < len(parts):
            body = parts[idx]
            idx += 1
        # notify-send -u critical title body
        while idx < len(parts):
            if parts[idx] in ("-u", "--urgency") and idx + 1 < len(parts):
                urgency = parts[idx + 1]
                idx += 2
            else:
                idx += 1
    elif parts[0].endswith("sensei-notify.sh"):
        if idx < len(parts):
            title = parts[idx]
            idx += 1
        if idx < len(parts):
            body = parts[idx]
            idx += 1
        if idx < len(parts):
            urgency = parts[idx]
    else:
        # Fallback: treat remainder as body, keep default title
        body = " ".join(parts[1:])
    if urgency not in ("low", "normal", "critical"):
        urgency = "normal"
    return title, body, urgency


# --- Registry -------------------------------------------------------------


class Registry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._register_phase1()

    def _register_phase1(self) -> None:
        self._capabilities["desktop.launch_app"] = Capability(
            name="desktop.launch_app",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "Bash command of the form `<app_name>` or "
                            "`<app_name> &`, where <app_name> is in "
                            "DESKTOP_APP_ALLOWLIST."
                        ),
                    }
                },
                "required": ["command"],
            },
            executor_name="master_ai.launch_desktop_app_safely",
            permission_tier="allow",
            risk_tier="low",
            cost_tier="cheap",
            verification_policy=VerificationPolicy(
                verifier_name="verifiers.verify_process_running",
                max_wait_s=5.0,
                poll_ms=500,
            ),
            retry_policy=RetryPolicy(max_retries=0),
            fallback_options=[],
            failure_messages={
                "blocked": "Refused to launch — capability blocked or app not in allowlist.",
                "verify_failed": "Launched but the process did not appear within the verification window.",
                "executor_error": "Executor raised an exception during launch.",
                "bridge_unreachable": "Local bridge is unreachable; cannot dispatch desktop.launch_app right now.",
            },
        )
        self._capabilities["desktop.notify"] = Capability(
            name="desktop.notify",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "urgency": {"type": "string"},
                },
                "required": ["title"],
            },
            executor_name="master_ai.notify_desktop",
            permission_tier="allow",
            risk_tier="low",
            cost_tier="cheap",
            verification_policy=VerificationPolicy(
                verifier_name="verifiers.verify_notification_sent",
                max_wait_s=1.0,
                poll_ms=100,
            ),
            retry_policy=RetryPolicy(max_retries=0),
            fallback_options=[],
            failure_messages={
                "blocked": "Refused to send notification — capability blocked.",
                "verify_failed": "Notification could not be verified as displayed.",
                "executor_error": "Executor raised an exception while sending notification.",
            },
        )

    def lookup(self, kind: str, args: str) -> Decision:
        """Match a directive (kind + args) to a registered capability.

        Returns Decision. capability=None signals the caller to use the
        existing generic dispatch path — this keeps the registry opt-in
        during phase 1.
        """
        if kind == "RUN":
            matched_app = _matches_desktop_launch(args)
            if matched_app:
                cap = self._capabilities.get("desktop.launch_app")
                return Decision(
                    allow=True,
                    requires_confirmation=False,
                    reason=f"matched desktop.launch_app for '{matched_app}'",
                    capability=cap,
                    verify_target=matched_app,
                )
            # Intercept bare `notify-send ...` directives emitted by older
            # prompts and route them through the verified wrapper instead.
            stripped = (args or "").strip()
            if stripped.startswith("notify-send") or stripped.startswith("sensei-notify"):
                cap = self._capabilities.get("desktop.notify")
                title, body, urgency = _parse_notify_args(stripped)
                return Decision(
                    allow=True,
                    requires_confirmation=False,
                    reason="matched desktop.notify from RUN: directive",
                    capability=cap,
                    verify_target=None,
                )
        return Decision(
            allow=True,
            requires_confirmation=False,
            reason="no specific capability matched — generic dispatch",
            capability=None,
        )

    def get(self, name: str) -> Optional[Capability]:
        return self._capabilities.get(name)

    def names(self) -> list[str]:
        return sorted(self._capabilities.keys())


# Module-level singleton — avoids constructing the registry on every import
_REGISTRY: Optional[Registry] = None


def get_registry() -> Registry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = Registry()
    return _REGISTRY


__all__ = [
    "REGISTRY_VERSION",
    "SAFETY_POLICY_VERSION",
    "RetryPolicy",
    "VerificationPolicy",
    "Capability",
    "Decision",
    "DESKTOP_APP_ALLOWLIST",
    "Registry",
    "get_registry",
]
