"""Fetch and normalize user records."""


def fetch_user(raw):
    """Return normalized dict from raw input."""
    name = (
        raw.get("name", "").strip().title()
    )  # BUG: title() lowercases already-camel names
    age = raw.get("age", 0)
    if isinstance(age, str):
        age = int(age)  # BUG: does not strip whitespace
    return {"name": name, "age": age}
