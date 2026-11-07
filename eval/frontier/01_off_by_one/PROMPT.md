# Task 01

There is an off-by-one bug in this repo. The API returns one extra item per page, and the `next_page` token calculation in `api.py` is therefore wrong. Fix the bug so `list_users` returns exactly `page_size` items per page and the next-page token is correct.

Repo: `repo/`
