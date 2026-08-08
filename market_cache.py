"""Small thread-safe TTL cache with stale fallback for market data."""

from __future__ import annotations

import threading
import time
from typing import Any


_lock = threading.Lock()
_items: dict[str, tuple[float, Any]] = {}


def put(key: str, value: Any) -> None:
    with _lock:
        _items[key] = (time.time(), value)


def get(key: str, max_age_seconds: int) -> tuple[Any | None, bool]:
    with _lock:
        item = _items.get(key)
    if not item:
        return None, False
    created_at, value = item
    age = time.time() - created_at
    if age > max_age_seconds:
        return None, False
    return value, age > 300
