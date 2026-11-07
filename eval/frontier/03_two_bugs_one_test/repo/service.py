"""Service orchestrator."""

from fetcher import fetch_user


def get_user_summary(raw):
    u = fetch_user(raw)
    return f"{u['name']} is {u['age']} years old"
