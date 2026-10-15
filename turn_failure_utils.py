def is_max_iteration_handoff(result: dict) -> bool:
    """
    Determine if the result represents a non-failed, non-interrupted max_iterations_reached handoff.
    This is a resumable boundary where the agent hit the iteration limit but still produced a summary.
    Used by cron delivery and /goal judge to agree on what counts as a non-failed handoff.
    """
    if not isinstance(result, dict):
        return False
    if result.get("failed") is True or result.get("interrupted") is True:
        return False
    if result.get("completed") is not False:
        return False
    reason = result.get("turn_exit_reason")
    if not (isinstance(reason, str) and reason.startswith("max_iterations_reached(")):
        return False
    final_response = result.get("final_response")
    return bool(final_response and str(final_response).strip())
