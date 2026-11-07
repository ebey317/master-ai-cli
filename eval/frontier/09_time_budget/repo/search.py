"""Search utilities."""


def find_duplicates(items):
    """Return a list of duplicate values."""
    dups = []
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if a == b and a not in dups:
                dups.append(a)
    return dups
