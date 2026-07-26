"""Live activity log — one stream for everything the app is doing.

An in-memory ring buffer plus fan-out to any connected listeners. Nothing is
persisted: this is a window onto the running process, not an audit trail.

`log()` is deliberately synchronous and never raises — logging must not be able
to break the thing it is reporting on.
"""
import asyncio
import itertools
from collections import deque
from datetime import datetime, timezone
from typing import Any

MAX_EVENTS = 600
QUEUE_SIZE = 200

LEVELS = ("info", "ok", "warn", "error")

_events: deque[dict] = deque(maxlen=MAX_EVENTS)
_seq = itertools.count(1)
_subscribers: set[asyncio.Queue] = set()


def log(source: str, message: str, level: str = "info", **extra: Any) -> dict:
    event = {
        "id": next(_seq),
        "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level if level in LEVELS else "info",
        "source": source,
        "message": str(message)[:400],
        **extra,
    }
    _events.append(event)
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # a listener that can't keep up loses events rather than blocking us
            pass
        except Exception:  # noqa: BLE001
            _subscribers.discard(queue)
    return event


def history(since: int = 0, limit: int = 300) -> list[dict]:
    items = [e for e in _events if e["id"] > since]
    return items[-limit:]


def clear() -> int:
    count = len(_events)
    _events.clear()
    return count


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def stats() -> dict:
    return {
        "buffered": len(_events),
        "capacity": MAX_EVENTS,
        "listeners": len(_subscribers),
        "latest_id": _events[-1]["id"] if _events else 0,
    }
