"""Flat entity message protocol for Sensei.

Design idea (ported from upstream kap-server "flat entity message protocol v3",
reimplemented — no upstream code copied):

- Every observable agent activity becomes a small FLAT message keyed by stable
  entity ids (`session_id`, `turn_id`, `step_id`, `tool_call_id`), a monotonic
  `seq`, and a `type`. No nested opaque blobs.
- Messages are append-only in a per-session journal, so a late subscriber can
  REPLAY the exact stream via `history()` (paged by turn, largest-turn-safe).
- Live clients subscribe over the existing hook bus; no UI logic is embedded.

Sensei wiring (no core edits needed):
  from scripts.entity_events import EntityEventBus
  bus = EntityEventBus(session_id)
  hooks.register("on_turn_start",  lambda ev: bus.turn(ev))
  hooks.register("on_step",        lambda ev: bus.step(ev))
  hooks.register("on_tool_start",  lambda ev: bus.tool_call(ev))
  hooks.register("on_tool_end",    lambda ev: bus.tool_result(ev))
  hooks.register("on_delta",       lambda ev: bus.delta(ev))
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterable
from typing import Any

TYPES = (
    "session.state",
    "turn.start",
    "turn.end",
    "step.snapshot",
    "assistant.delta",
    "thinking.delta",
    "tool_call.snapshot",
    "tool_call.delta",
    "tool_call.result",
    "agent.state",
    "error",
    "ack",
)
PAGE_BYTES = 256 * 1024  # page budget; oversized turns stay reachable
MAX_MESSAGES_PER_PAGE = 500


class EntityEventBus:
    """Append-only flat-entity journal for one session."""

    def __init__(self, session_id: str, journal_dir: str = ".sensei/events"):
        self.session_id = session_id
        self._dir = os.path.join(journal_dir, session_id)
        os.makedirs(self._dir, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = self._load_last_seq()
        self._subscribers: list[Any] = []  # callable(type, dict) -> None

    # ------------------------------ emit --------------------------------

    def emit(
        self, type: str, payload: dict[str, Any], turn_id: str | None = None, **ids: Any
    ) -> dict[str, Any]:
        # **ids values come straight from ev.get(...) at call sites, so they
        # are legitimately Any | None (None = "absent"). Only truthy values
        # are copied onto the message below, so a strict `str` annotation
        # here just lied about the contract.
        if type not in TYPES:
            type = "error"
        with self._lock:
            self._seq += 1
            msg = {
                "id": f"{type}:{turn_id or 'session'}:{self._seq}",
                "v": 1,
                "seq": self._seq,
                "type": type,
                "ts": time.time(),
                "session_id": self.session_id,
                "turn_id": turn_id or ids.get("turn_id", ""),
            }
            for key in ("step_id", "tool_call_id", "prompt_id"):
                if key in ids and ids[key]:
                    msg[key] = ids[key]
            msg.update(payload)
            self._append(msg)
        for sub in list(self._subscribers):
            try:
                sub(msg["type"], msg)
            except Exception:
                pass  # a bad subscriber must not corrupt the journal
        return msg

    def subscribe(self, fn: Any) -> Any:
        self._subscribers.append(fn)
        return fn

    def unsubscribe(self, fn: Any) -> None:
        if fn in self._subscribers:
            self._subscribers.remove(fn)

    # --------------------------- convenience ----------------------------

    def turn(self, ev: dict[str, Any]) -> None:
        self.emit(
            "turn.start",
            {"state": "running"},
            turn_id=ev.get("turn_id", self._now_id()),
        )

    def step(self, ev: dict[str, Any]) -> None:
        self.emit(
            "step.snapshot",
            {"state": ev.get("state", "running")},
            turn_id=ev.get("turn_id"),
            step_id=ev.get("step_id"),
        )

    def tool_call(self, ev: dict[str, Any]) -> None:
        self.emit(
            "tool_call.snapshot",
            {
                "name": ev.get("tool", ""),
                "input": ev.get("input", {}),
                "state": "running",
            },
            turn_id=ev.get("turn_id"),
            step_id=ev.get("step_id"),
            tool_call_id=ev.get("tool_call_id") or self._now_id(),
        )

    def tool_result(self, ev: dict[str, Any]) -> None:
        self.emit(
            "tool_call.result",
            {
                "state": ev.get("ok", True) and "completed" or "failed",
                "output": ev.get("output"),
            },
            turn_id=ev.get("turn_id"),
            step_id=ev.get("step_id"),
            tool_call_id=ev.get("tool_call_id", ""),
        )

    def delta(self, ev: dict[str, Any]) -> None:
        if not (ev.get("text") or "").strip():
            return  # filter empty deltas (upstream "filter empty thinking")
        self.emit(
            ev.get("kind", "assistant") + ".delta",
            {"text": ev["text"]},
            turn_id=ev.get("turn_id"),
            step_id=ev.get("step_id"),
        )

    def agent_state(self, state: str, turn_id: str = "") -> None:
        self.emit("agent.state", {"state": state}, turn_id=turn_id)

    # ------------------------------ history -----------------------------

    def history(self, after_seq: int = 0) -> dict[str, Any]:
        """Replay journal after `after_seq`, page by turn.

        Returns {"messages": [...], "last_seq": N, "has_more": bool}.
        Paging breaks at turn boundaries so oversized turns never strand
        later messages behind an empty page (upstream fix).
        """
        msgs = [m for m in self._read_all() if m["seq"] > after_seq]
        page: list[dict[str, Any]] = []
        nbytes = 0
        seen: dict[str, bool] = {}
        for m in msgs:
            if page and m["turn_id"] != page[-1]["turn_id"]:
                if nbytes >= PAGE_BYTES or len(page) >= MAX_MESSAGES_PER_PAGE:
                    break
            page.append(m)
            nbytes += len(json.dumps(m))
            if nbytes >= PAGE_BYTES and not seen.get(m["turn_id"]):
                seen[m["turn_id"]] = True
        has_more = bool(msgs and page and page[-1]["seq"] < msgs[-1]["seq"])
        return {
            "messages": page,
            "last_seq": page[-1]["seq"] if page else after_seq,
            "has_more": has_more,
        }

    # ------------------------------ storage -----------------------------

    def _path(self) -> str:
        return os.path.join(self._dir, "journal.jsonl")

    def _append(self, msg: dict[str, Any]) -> None:
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def _read_all(self) -> Iterable[dict[str, Any]]:
        try:
            with open(self._path(), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        yield json.loads(line)
        except FileNotFoundError:
            return

    def _load_last_seq(self) -> int:
        last = 0
        for m in self._read_all():
            last = max(last, m["seq"])
        return last

    @staticmethod
    def _now_id() -> str:
        return time.strftime("%Y%m%dT%H%M%S")
