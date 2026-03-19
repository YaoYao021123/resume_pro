"""Thread-safe in-memory generation log buffer.

Used by generate_resume.py to emit events and consumed by the /monitor
HTML page via the /api/monitor/logs polling endpoint.
"""
import threading
import time
from collections import deque
from typing import Any

_lock = threading.Lock()
_entries: deque = deque(maxlen=1000)
_seq = 0


def emit(category: str, text: str, *, data: Any = None) -> int:
    """Append a log entry and return its sequence number."""
    global _seq
    with _lock:
        _seq += 1
        entry: dict[str, Any] = {
            'seq': _seq,
            'ts': time.time(),
            'category': category,
            'text': text,
        }
        if data is not None:
            entry['data'] = data
        _entries.append(entry)
        return _seq


def get_entries_since(seq: int) -> list[dict]:
    """Return all entries with seq > given seq."""
    with _lock:
        return [e for e in _entries if e['seq'] > seq]


def get_all() -> list[dict]:
    with _lock:
        return list(_entries)


def clear() -> None:
    global _seq
    with _lock:
        _entries.clear()
        _seq = 0
