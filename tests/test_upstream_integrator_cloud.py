"""upstream_integrator: free-model chain; an outage never retires a commit."""

import importlib.util
import io
import urllib.error
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "upstream_integrator",
    Path(__file__).resolve().parent.parent / "scripts" / "upstream_integrator.py",
)
assert spec is not None and spec.loader is not None
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


def test_every_model_in_the_chain_is_free():
    assert ui.FREE_MODELS and all(m.endswith(":free") for m in ui.FREE_MODELS)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_dead_model_is_skipped_for_the_next(monkeypatch):
    monkeypatch.setattr(ui, "_openrouter_key", lambda: "k")
    monkeypatch.setattr(ui.time, "sleep", lambda s: None)
    seen = []

    def fake(req, timeout=0):
        model = __import__("json").loads(req.data)["model"]
        seen.append(model)
        if len(seen) == 1:
            raise urllib.error.HTTPError(req.full_url, 404, "gone", {}, None)
        return _Resp(
            b'{"choices":[{"message":{"content":"a real plan"}}],"usage":{"completion_tokens":5}}'
        )

    monkeypatch.setattr(ui.urllib.request, "urlopen", fake)
    assert ui._ask_cloud([{"role": "user", "content": "x"}]) == "a real plan"
    assert seen == ui.FREE_MODELS[:2]


def test_total_outage_returns_none_not_a_failure(monkeypatch):
    monkeypatch.setattr(ui, "_openrouter_key", lambda: "k")
    monkeypatch.setattr(ui.time, "sleep", lambda s: None)

    def boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 429, "limit", {}, None)

    monkeypatch.setattr(ui.urllib.request, "urlopen", boom)
    assert ui._ask_cloud([{"role": "user", "content": "x"}]) is None
