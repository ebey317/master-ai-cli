# Task 04

`helpers.py` and `validators.py` contain duplicated validation logic. Refactor so `api.py` imports from a single source of truth (`validators.py`) and remove `helpers.py`. The visible behavior of `register()` must stay identical.

Repo: `repo/`
