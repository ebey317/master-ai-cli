import turn_failure_utils


def test_is_max_iteration_handoff():
    """Test the max_iteration_handoff predicate matches upstream behavior."""
    # Valid handoff case
    assert (
        turn_failure_utils.is_max_iteration_handoff(
            {
                "failed": False,
                "interrupted": False,
                "completed": False,
                "turn_exit_reason": "max_iterations_reached(5/5)",
                "final_response": "Task summary",
            }
        )
        is True
    )

    # Empty final response -> not a handoff
    assert (
        turn_failure_utils.is_max_iteration_handoff(
            {
                "failed": False,
                "interrupted": False,
                "completed": False,
                "turn_exit_reason": "max_iterations_reached(5/5)",
                "final_response": "",
            }
        )
        is False
    )

    # Failed true -> not a handoff
    assert (
        turn_failure_utils.is_max_iteration_handoff(
            {
                "failed": True,
                "interrupted": False,
                "completed": False,
                "turn_exit_reason": "max_iterations_reached(5/5)",
                "final_response": "Task summary",
            }
        )
        is False
    )

    # Interrupted true -> not a handoff
    assert (
        turn_failure_utils.is_max_iteration_handoff(
            {
                "failed": False,
                "interrupted": True,
                "completed": False,
                "turn_exit_reason": "max_iterations_reached(5/5)",
                "final_response": "Task summary",
            }
        )
        is False
    )

    # Completed true -> not a handoff
    assert (
        turn_failure_utils.is_max_iteration_handoff(
            {
                "failed": False,
                "interrupted": False,
                "completed": True,
                "turn_exit_reason": "max_iterations_reached(5/5)",
                "final_response": "Task summary",
            }
        )
        is False
    )

    # Completed None (not False) -> not a handoff
    assert (
        turn_failure_utils.is_max_iteration_handoff(
            {
                "failed": False,
                "interrupted": False,
                "completed": None,
                "turn_exit_reason": "max_iterations_reached(5/5)",
                "final_response": "Task summary",
            }
        )
        is False
    )

    # Wrong turn_exit_reason -> not a handoff
    assert (
        turn_failure_utils.is_max_iteration_handoff(
            {
                "failed": False,
                "interrupted": False,
                "completed": False,
                "turn_exit_reason": "other_reason",
                "final_response": "Task summary",
            }
        )
        is False
    )

    # Non-string turn_exit_reason -> not a handoff
    assert (
        turn_failure_utils.is_max_iteration_handoff(
            {
                "failed": False,
                "interrupted": False,
                "completed": False,
                "turn_exit_reason": 123,
                "final_response": "Task summary",
            }
        )
        is False
    )

    # Non-dict input -> not a handoff
    assert turn_failure_utils.is_max_iteration_handoff("invalid") is False
    assert turn_failure_utils.is_max_iteration_handoff(None) is False
    assert turn_failure_utils.is_max_iteration_handoff([]) is False

    print("All tests passed.")


if __name__ == "__main__":
    test_is_max_iteration_handoff()
