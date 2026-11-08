# Task 07

The prompt includes this stack trace from a failing run:
```
Traceback (most recent call last):
  File "runner.py", line 6, in run_task
    clean_id = task_id[1:] if task_id.startswith("t-") else task_id
ValueError: invalid literal for int() in handler for task t-42
```
(The actual failure is that `runner.py` strips the leading `t-` prefix incorrectly, leaving `-42`, which `handlers.py` later tries to parse.) Fix `runner.py` so it removes the `t-` prefix entirely, not just the first character.

Repo: `repo/`
