"""suggestive_notify.py — the phone half of Suggestive Thinking.

Lijah's design law (2026-10-25, HANDOFF.md): the suggestive-thinking loop
— notice -> suggest -> notify with the decision attached -> approve/pass
-> execute — is NATIVE to Master AI, MODEL-AGNOSTIC, and NOT PIPED
through any outside agent or service. This module is that law as code:

- No model is consulted anywhere in here. Delivery, decision handling,
  and the unseen-digest are deterministic framework code, so the loop
  behaves identically no matter which model is running underneath.
- DELIVER (suggestive_thinking_sources.deliver_default) calls
  notify_candidate() for every surfaced candidate. The operator gets a
  Telegram message — via Master AI's own bot, sent with telegram_client
  — whose inline buttons CARRY THE DECISION: "Do it" queues nothing new,
  it approves the approval_queue.py entry DELIVER already created, whose
  registered handler then actually runs the work. "We're good" rejects
  that entry. A bare notification with no action attached is the defect
  this module exists to remove.
- A suggestion that gets no tap within UNSEEN_TTL_HOURS is not nagged
  about. It goes quiet and resurfaces exactly once per
  DIGEST_COOLDOWN_HOURS inside ONE batched digest message, per the
  approval spec ("here are three things that I brought to your
  attention that didn't get looked at").

State lives next to the loop's own log:
    ~/master-ai-cli/memory/suggestive-notify-state.json

CLI:
    python3 suggestive_notify.py digest    # send the unseen digest if due
    python3 suggestive_notify.py pending   # list awaiting/unseen items
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path

STATE_PATH = Path.home() / "master-ai-cli" / "memory" / "suggestive-notify-state.json"

UNSEEN_TTL_HOURS = 12  # no tap within this window -> item goes "unseen"
DIGEST_COOLDOWN_HOURS = 20  # at most one digest per this window

_APPROVE = "approve"
_PASS = "pass"

# ── executor / model naming (the card's Who, made honest) ────────────
#
# Lijah's routing policy (2026-10-25): work behind these cards runs on
# the cheapest models first — free lanes (NVIDIA, OpenRouter free,
# Poolside) before anything paid — and only a huge job escalates. The
# card must NAME the model doing the work, so resolve_work_model() is
# the single sanctioned way to pick one: any future model-backed
# executor in this loop calls it instead of naming a model itself.
# Today's registered executors are all deterministic code (no model at
# all — cheaper than free), and the card says so plainly.

DETERMINISTIC_HANDLERS = {"repo_test_check", "grant_packet", "money_org_check"}

FREE_PICKER_CACHE = Path.home() / ".master_ai_free_model_picker_cache.json"
ACTIVE_MODEL_FILE = Path.home() / ".master_ai_active_model"
FALLBACK_ORDER_FILE = Path.home() / ".master_ai_fallback_order.json"


def _free_models_from_cache() -> list[dict]:
    try:
        data = json.loads(FREE_PICKER_CACHE.read_text(encoding="utf-8"))
        models = data.get("models") or []
    except Exception:
        return []
    free = []
    for m in models:
        pricing = m.get("pricing") or {}
        if pricing.get("prompt") == "0" and pricing.get("completion") == "0":
            free.append(m)
    return free


def resolve_work_model(*, huge: bool = False) -> str | None:
    """The model a model-backed approval would run on, per the
    free/cheap-first policy: the free model picker's top-ranked free
    model; the operator's active model and the fallback-order's first
    lane are the fallbacks when no free catalog is readable.
    huge=True is the one escalation: the active model directly."""
    if not huge:
        free = _free_models_from_cache()
        if free:
            try:
                import free_model_picker

                ranked = free_model_picker.rank_free_models(free)
                if ranked:
                    return ranked[0].get("id")
            except Exception:
                pass
            return free[0].get("id")
    try:
        active = ACTIVE_MODEL_FILE.read_text(encoding="utf-8").strip()
        if active:
            return active
    except Exception:
        pass
    try:
        order = json.loads(FALLBACK_ORDER_FILE.read_text(encoding="utf-8"))
        if isinstance(order, list) and order:
            return str(order[0])
    except Exception:
        pass
    return None


def describe_executor(action_type: str) -> str:
    """The card's Model line: what actually does the work if the
    operator taps Do it."""
    if not action_type:
        return "no AI model — no worker attached; tapping Do it only acknowledges"
    if action_type in DETERMINISTIC_HANDLERS:
        return "no AI model — plain code built into Master AI (costs $0)"
    model = resolve_work_model()
    if model:
        return f"{model} — free/cheap first, by Master AI's own routing (NVIDIA · OpenRouter free · Poolside)"
    return "free/cheap first, by Master AI's own routing (NVIDIA · OpenRouter free · Poolside)"


_handlers_loaded = False


# ── state store ───────────────────────────────────────────────────────


def _load_state(state_path: Path = STATE_PATH) -> dict:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "items" in data:
            return data
    except Exception:
        pass
    return {"items": {}, "last_digest_ts": 0.0}


def _save_state(state: dict, state_path: Path = STATE_PATH) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(state_path)


def _new_id(existing_ids) -> str:
    from datetime import datetime

    base = datetime.now().strftime("%Y%m%d%H%M%S")
    if base not in existing_ids:
        return base
    for suffix in range(1, 100):
        candidate = f"{base}-{suffix:02d}"
        if candidate not in existing_ids:
            return candidate
    return base + "-xx"


# ── message construction (pure, testable) ─────────────────────────────


def build_keyboard(notify_id: str) -> dict:
    """The decision, attached to the notification itself."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Do it", "callback_data": f"sg:a:{notify_id}"},
                {"text": "👍 We're good", "callback_data": f"sg:p:{notify_id}"},
            ]
        ]
    }


def format_message(card: dict) -> str:
    """Render the full who/what/where/when/why/how card (Lijah,
    2026-10-25: the thin question-only card was not enough — and 'who'
    means which model is doing the work). telegram_client sends with
    parse_mode=HTML, so every operator-facing value is escaped here —
    a headline containing '<' must not break the send or smuggle
    markup."""

    def esc(v) -> str:
        return html.escape(str(v or ""))

    lines = ["💡 <b>Master AI suggestion</b>"]
    if card.get("headline"):
        lines.append(f"<b>{esc(card['headline'])}</b>")
    lines.append("")
    lines.append(f"<b>Who:</b> {esc(card.get('who'))}")
    if card.get("executor"):
        lines.append(f"<b>Executor:</b> {esc(card['executor'])}")
    lines.append(f"<b>Model:</b> {esc(card.get('model'))}")
    lines.append(f"<b>What:</b> {esc(card.get('what'))}")
    lines.append(f"<b>Where:</b> {esc(card.get('where'))}")
    lines.append(f"<b>When:</b> {esc(card.get('when'))}")
    lines.append(f"<b>Why:</b> {esc(card.get('why'))}")
    lines.append(f"<b>How:</b> {esc(card.get('how'))}")
    return "\n".join(lines)


_PLAIN_WORKERS = {
    "repo_test_check": "Master AI's built-in test-suite runner",
    "grant_packet": "Master AI's built-in grant-packet builder",
    "money_org_check": "Master AI's built-in money-item checker",
}

_PLAIN_ACTIONS = {
    "repo_test_check": "run the test suite for the project named above and send you the pass/fail result right here",
    "grant_packet": "put together the ready-to-submit packet for the grant named above and send it to you right here",
    "money_org_check": "check the money item named above, organize it, and send you what it finds right here",
}

_PLAIN_ACTION_FALLBACK = (
    "do the work described above and send you the result right here"
)


def _plain_where(source) -> str:
    """Plain-English 'where' for a candidate source key (Lijah,
    2026-10-26: the cards speak plain English — no machine keys,
    no code names)."""
    src = str(source or "")
    if src.startswith("stale-project:"):
        return f"the {src.split(':', 1)[1]} project on Mary"
    if src.startswith("grant-deadline:"):
        return "your grants list"
    if src.startswith("money-org:"):
        return "your money list"
    if src.startswith("gmail-unread:"):
        return "your Gmail inbox"
    if src.startswith("email-bridge:"):
        return "your email"
    if src == "sensei-chat-stale":
        return "your chats with Master AI"
    if src.startswith("upstream-proposal:"):
        return "Master AI's own list of improvement ideas"
    tail = src.split(":", 1)[-1] if ":" in src else src
    return tail.replace("-", " ").replace("_", " ").strip() or "Master AI on Mary"


def default_card(candidate, entry_id: str | None = None) -> dict:
    """Build the card from a Candidate using the same field mapping
    DELIVER uses for the approval_queue entry, so the phone card and
    the desktop queue never tell two different stories."""
    action_type = getattr(candidate, "action_type", "")
    question = getattr(candidate, "question", "") or getattr(candidate, "headline", "")
    if action_type:
        how = (
            "If you tap \u2705 Do it, Master AI will "
            + _PLAIN_ACTIONS.get(action_type, _PLAIN_ACTION_FALLBACK)
            + ". If you tap \U0001f44d We're good, nothing happens."
        )
    else:
        how = question
    return {
        "headline": getattr(candidate, "headline", ""),
        "who": "Master AI — your own framework, running on Mary",
        "executor": _PLAIN_WORKERS.get(action_type)
        or (action_type.replace("_", " ") if action_type else "none attached"),
        "model": describe_executor(action_type),
        "what": getattr(candidate, "deliverable", "")
        or getattr(candidate, "headline", ""),
        "where": _plain_where(getattr(candidate, "source", "")),
        "when": "As soon as you tap \u2705 Do it",
        "why": getattr(candidate, "detail", "") or getattr(candidate, "headline", ""),
        "how": how,
    }


def parse_callback(data: str) -> tuple[str, str] | None:
    """'sg:a:<id>' -> ('approve', '<id>'); 'sg:p:<id>' -> ('pass', '<id>')."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "sg":
        return None
    if parts[1] == "a":
        return _APPROVE, parts[2]
    if parts[1] == "p":
        return _PASS, parts[2]
    return None


# ── delivery ──────────────────────────────────────────────────────────


def _default_send(text: str, reply_markup: dict | None = None) -> dict:
    import telegram_client

    return telegram_client.send_message(None, text, reply_markup=reply_markup)


def notify_candidate(
    candidate,
    entry_id: str | None = None,
    *,
    card: dict | None = None,
    send=None,
    state_path: Path = STATE_PATH,
    now: float | None = None,
) -> dict:
    """Send one surfaced candidate to the operator's phone with its
    decision attached, and record it as awaiting that decision.

    `candidate` is a suggestive_thinking.Candidate (duck-typed: needs
    question/headline/deliverable/detail/source). `entry_id` is the
    approval_queue entry DELIVER queued for this candidate, or None for
    a pure-information candidate — its "Do it" then just acknowledges.
    Never raises: returns the record with status 'send_failed' if the
    send itself failed, so the caller can log and move on.
    """
    send = send or _default_send
    now = time.time() if now is None else now
    state = _load_state(state_path)
    notify_id = _new_id(set(state["items"]))
    question = getattr(candidate, "question", "") or getattr(candidate, "headline", "")
    card = card or default_card(candidate, entry_id)
    record = {
        "id": notify_id,
        "entry_id": entry_id,
        "source": getattr(candidate, "source", ""),
        "question": question,
        "deliverable": getattr(candidate, "deliverable", ""),
        "detail": getattr(candidate, "detail", ""),
        "card": card,
        "status": "awaiting",
        "sent_ts": now,
        "decided_ts": None,
        "message_id": None,
        "in_digest_ts": None,
    }
    result = send(
        format_message(card),
        reply_markup=build_keyboard(notify_id),
    )
    if result.get("ok"):
        record["message_id"] = result.get("message_id")
    else:
        record["status"] = "send_failed"
        record["error"] = result.get("error") or "unknown send error"
    state["items"][notify_id] = record
    _save_state(state, state_path)
    return record


# ── decision handling (called by telegram_gateway's callback handler) ──


def _ensure_handlers() -> None:
    """Make approval_queue.approve() able to execute suggestive entries
    inside a long-lived consumer process (the Telegram gateway).

    Mirrors approval_queue._load_handlers' explicit-path load: the
    suggestive handlers (grant_packet, money_org_check, repo_test_check)
    live in suggestive_thinking_sources.py, loaded from its canonical
    ~/scripts path so name resolution can't settle on a stale copy.
    """
    global _handlers_loaded
    if _handlers_loaded:
        return
    _handlers_loaded = True
    try:
        import importlib.util
        import sys

        path = Path.home() / "scripts" / "suggestive_thinking_sources.py"
        if not path.exists():
            path = Path(__file__).resolve().parent / "suggestive_thinking_sources.py"
        existing = sys.modules.get("suggestive_thinking_sources")
        if existing is not None and getattr(existing, "__file__", None) != str(
            path.resolve()
        ):
            del sys.modules["suggestive_thinking_sources"]
        spec = importlib.util.spec_from_file_location(
            "suggestive_thinking_sources", str(path.resolve())
        )
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    except Exception:
        pass  # approve() will report APPROVED_NO_HANDLER honestly if so


def _default_approve(entry_id: str):
    _ensure_handlers()
    import approval_queue

    return approval_queue.approve(entry_id)


def _default_reject(entry_id: str):
    import approval_queue

    return approval_queue.reject(entry_id)


def handle_decision(
    notify_id: str,
    decision: str,
    *,
    state_path: Path = STATE_PATH,
    approve_fn=None,
    reject_fn=None,
    now: float | None = None,
) -> tuple[bool, str]:
    """Apply the operator's tap. Returns (ok, text-for-the-message-edit).

    Approve runs the queued approval_queue entry's handler — the work
    actually executes. Pass rejects the entry; the suggestion dies
    silently, no nagging. Deciding twice is a no-op that reports the
    first decision (idempotent — double-taps happen).
    """
    now = time.time() if now is None else now
    state = _load_state(state_path)
    record = state["items"].get(notify_id)
    if record is None:
        return False, "That suggestion is no longer on file."
    if record["status"] in ("approved", "passed"):
        return True, f"Already decided: {record['status']}."
    if record["status"] == "send_failed":
        return False, "That suggestion never reached you — nothing to decide."

    entry_id = record.get("entry_id")
    if decision == _APPROVE:
        record["status"] = "approved"
        record["decided_ts"] = now
        _save_state(state, state_path)
        if entry_id:
            ok, msg = (approve_fn or _default_approve)(entry_id)
            return True, f"✅ Do it — approved.\n{msg}"
        return True, "✅ Do it — noted. (No queued action came with this one.)"
    if decision == _PASS:
        record["status"] = "passed"
        record["decided_ts"] = now
        _save_state(state, state_path)
        if entry_id:
            (reject_fn or _default_reject)(entry_id)
        return True, "👍 We're good — dropped it."
    return False, f"Unknown decision: {decision!r}"


# ── the unseen digest ─────────────────────────────────────────────────


def collect_unseen(
    state: dict, now: float, ttl_hours: float = UNSEEN_TTL_HOURS
) -> list[dict]:
    """Awaiting items past their TTL become 'unseen' (mutates state).
    Decided and send-failed items never resurface."""
    cutoff = now - ttl_hours * 3600
    unseen = []
    for record in state["items"].values():
        if record["status"] == "awaiting" and record["sent_ts"] <= cutoff:
            record["status"] = "unseen"
        if record["status"] == "unseen":
            unseen.append(record)
    return unseen


def build_digest(unseen: list[dict]) -> tuple[str, dict]:
    """ONE message for the whole batch, each item still carrying its
    own decision buttons."""
    lines = [
        f"🗂 <b>Since earlier</b> — {len(unseen)} thing(s) I brought to your "
        "attention that didn't get looked at:"
    ]
    rows = []
    for i, record in enumerate(unseen, start=1):
        lines.append(f"\n{i}. {html.escape(record['question'])}")
        rows.append(
            [
                {"text": f"✅ Do {i}", "callback_data": f"sg:a:{record['id']}"},
                {"text": f"👍 Pass {i}", "callback_data": f"sg:p:{record['id']}"},
            ]
        )
    return "\n".join(lines), {"inline_keyboard": rows}


def maybe_send_digest(
    *,
    send=None,
    state_path: Path = STATE_PATH,
    now: float | None = None,
    ttl_hours: float = UNSEEN_TTL_HOURS,
    cooldown_hours: float = DIGEST_COOLDOWN_HOURS,
) -> dict | None:
    """Resurface everything unseen as one batched digest, at most once
    per cooldown window. Returns the digest record summary, or None if
    nothing was due. Never raises."""
    send = send or _default_send
    now = time.time() if now is None else now
    state = _load_state(state_path)
    unseen = collect_unseen(state, now, ttl_hours)
    _save_state(state, state_path)
    if not unseen:
        return None
    last_digest = float(state.get("last_digest_ts") or 0)
    if last_digest and now - last_digest < cooldown_hours * 3600:
        return None
    text, keyboard = build_digest(unseen)
    result = send(text, reply_markup=keyboard)
    if not result.get("ok"):
        return None
    state = _load_state(state_path)
    for record in unseen:
        live = state["items"].get(record["id"])
        if live and live["status"] == "unseen":
            live["in_digest_ts"] = now
    state["last_digest_ts"] = now
    _save_state(state, state_path)
    return {
        "digest_of": [r["id"] for r in unseen],
        "message_id": result.get("message_id"),
    }


# ── CLI ───────────────────────────────────────────────────────────────


def _cli() -> int:
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "digest":
        sent = maybe_send_digest()
        print(f"digest sent: {sent}" if sent else "no digest due")
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "pending":
        state = _load_state()
        open_items = [
            r for r in state["items"].values() if r["status"] in ("awaiting", "unseen")
        ]
        if not open_items:
            print("(nothing awaiting a decision)")
            return 0
        for r in open_items:
            print(f"  [{r['id']}] {r['status']:8s} {r['question']}")
        return 0
    print("usage: suggestive_notify.py {digest|pending}")
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
