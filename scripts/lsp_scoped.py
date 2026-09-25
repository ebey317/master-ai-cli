"""Per-profile LSP service registry.

``lsp.enabled``, server list and idle timeout are profile-scoped settings, so the
first routed profile must not decide whether every other profile gets
diagnostics.
"""

from __future__ import annotations

import threading

from . import profile_scope

_LOCK = threading.Lock()
_SERVICES: dict[str, object] = {}


class LSPServiceStub:
    def __init__(self, profile: profile_scope.Profile):
        self.profile = profile
        self.enabled = profile_scope.config_value(
            "lsp", "enabled", default=False, profile=profile
        )

    def is_active(self) -> bool:
        return bool(self.enabled)


def get_service() -> LSPServiceStub | None:
    key = profile_scope.profile_key()
    with _LOCK:
        if key not in _SERVICES:
            _SERVICES[key] = LSPServiceStub(profile_scope.current())
        return _SERVICES[key]
