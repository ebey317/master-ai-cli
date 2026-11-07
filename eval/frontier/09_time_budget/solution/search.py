"""Search utilities."""


def find_duplicates(items):
    """Return duplicate values in order of first duplicate occurrence."""
    seen = set()
    dups = []
    for x in items:
        if x in seen and x not in dups:
            dups.append(x)
        seen.add(x)
    return dups
