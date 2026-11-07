"""API layer that uses SliceStore."""

from data_store import SliceStore


def list_users(store: SliceStore, page_size: int = 10, page: int = 1):
    """Return a page of users and the next page token."""
    rows = store.page(page_size, page)
    next_page = page + 1 if len(rows) == page_size else None
    return rows, next_page
