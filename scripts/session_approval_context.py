"""Helper to determine if current turn is a local session's own foreground turn for execute_code approval skipping."""

def is_local_session_own_turn(
    session_policy_source: str,
    is_delegated_task: bool,
    has_approval_notifier: bool
) -> bool:
    """
    Return True if the current turn should skip whole-script execute_code approval.
    
    Conditions:
    - Session launched from CLI/TUI/GUI/ACP (frozen launch policy source)
    - Turn is not a delegated task child (sync or async)
    - Approval notifier is registered for the bound session key
    
    Args:
        session_policy_source: Source of session's frozen launch policy (e.g., 'cli', 'tui')
        is_delegated_task: True if turn is a delegate_task child
        has_approval_notifier: True if approval notifier registered for session key
        
    Returns:
        True if whole-script execute_code approval should be skipped
    """
    local_sources = {'cli', 'tui', 'gui', 'acp'}
    return (
        session_policy_source in local_sources
        and not is_delegated_task
        and has_approval_notifier
    )
