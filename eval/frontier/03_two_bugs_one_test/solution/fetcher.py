"""Fetch and normalize user records."""


def fetch_user(raw):
    """Return normalized dict from raw input."""
    name = raw.get("name", "").strip()  # fixed: preserve provided casing
    age = raw.get("age", 0)
    if isinstance(age, str):
        age = int(age.strip())  # fixed
    return {"name": name, "age": age}
