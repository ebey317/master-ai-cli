"""Tests for interruptible connector batch execution."""

from scripts.tools.connector_batch import BatchResult, run_batch


def test_stop_halts_before_remaining_entries():
    calls = [
        ("connectors__gmail__SEND_EMAIL", {"to": "a@example.com", "subject": "hi"}),
        ("connectors__linear__CREATE_ISSUE", {"title": "bug"}),
        ("connectors__notion__APPEND_BLOCK", {"page_id": "123", "text": "note"}),
    ]
    executed: list[tuple[str, dict]] = []
    interrupted = {"value": False}

    def execute(name: str, args: dict) -> str:
        executed.append((name, args))
        # Flip to True only once the first call has actually run, so the
        # first entry is seen as un-interrupted. Starting the flag at True
        # (as this test used to) contradicted its own assertions: run_batch
        # checks is_interrupted() BEFORE each entry, so entry 0 would be
        # marked INTERRUPTED rather than the OK the test then expected.
        interrupted["value"] = True
        return "ok"

    def is_interrupted() -> bool:
        return interrupted["value"]

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
