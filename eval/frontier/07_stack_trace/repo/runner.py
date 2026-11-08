"""Task runner."""


def run_task(task_id, handler):
    """Run a handler and return its result."""
    # BUG: strips leading character from task_id
    clean_id = task_id[1:] if task_id.startswith("t-") else task_id
    return handler(clean_id)
