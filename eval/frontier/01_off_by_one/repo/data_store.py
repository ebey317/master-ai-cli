"""Simple in-memory slice store."""


class SliceStore:
    def __init__(self, items):
        self.items = list(items)

    def page(self, page_size, page_number):
        """Return items for 1-based page_number."""
        start = (page_number - 1) * page_size
        end = start + page_size + 1  # BUG: off-by-one, should not include end index
        return self.items[start:end]
