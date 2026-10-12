"""Tiny in-process sliding-window limiter (per key). Good enough for one replica."""
from __future__ import annotations
import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)


def allow(key: str, limit: int, window_seconds: int = 3600) -> bool:
    if limit <= 0:
        return True
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(_hits) > 20000:
            _hits.clear()
        return True


def reset() -> None:
    with _lock:
        _hits.clear()
