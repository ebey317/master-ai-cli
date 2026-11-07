import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo"))

from api import list_users
from data_store import SliceStore


def test_page_size_exact():
    store = SliceStore(range(25))
    rows, nxt = list_users(store, page_size=10, page=1)
    assert len(rows) == 10
    assert rows == list(range(10))
    assert nxt == 2


def test_last_page():
    store = SliceStore(range(25))
    rows, nxt = list_users(store, page_size=10, page=3)
    assert rows == [20, 21, 22, 23, 24]
    assert nxt is None


def test_next_page_token_when_partial():
    store = SliceStore(range(15))
    rows, nxt = list_users(store, page_size=10, page=2)
    assert rows == [10, 11, 12, 13, 14]
    assert nxt is None
