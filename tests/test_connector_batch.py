"""Tests for interruptible connector batch execution."""

from scripts.tools.connector_batch import BatchResult, run_batch


def test_stop_halts_before_remaining_entries():
    calls = [
        ("connectors__gmail__SEND_EMAIL", {"to": "a@example.com", "subject": "hi"}),
        ("connectors__linear__CREATE_ISSUE", {"title": "bug"}),
        ("connectors__notion__APPEND_BLOCK", {"page_id": "123", "text": "note"}),
    ]
    executed: list[tuple[str, dict]] = []

    def execute(name: str, args: dict) -> str:
        executed.append((name, args))
        return "ok"

    interrupted_after = {1: True}  # flip after first call

    def is_interrupted() -> bool:
        return interrupted_after[1]

    results = run_batch(calls, execute, is_interrupted)

    assert len(results) == 3
    assert results[0] == BatchResult(
        name="connectors__gmail__SEND_EMAIL",
        arguments=calls[0][1],
        ok=True,
        code="OK",
        value="ok",
    )
    assert results[1].code == "INTERRUPTED"
    assert results[2].code == "INTERRUPTED"
    assert executed == [calls[0]]
