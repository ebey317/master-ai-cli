"""Task runner."""


def run_task(task_id, handler):
    """Run a handler and return its result."""
    clean_id = task_id[2:] if task_id.startswith("t-") else task_id  # fixed
    return handler(clean_id)
