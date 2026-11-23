"""In-process event bus: background work publishes small events, the /api/stream (SSE) clients receive them.

Events are hints ("something changed, refresh"), never the data itself, so a dropped event costs one
refresh and nothing else. Each listener has a bounded queue; a slow listener drops events, never blocks.
"""

import queue
import threading
import time

_lock = threading.Lock()
_subs = set()


def subscribe(maxsize=256):
    q = queue.Queue(maxsize=maxsize)
    with _lock:
        _subs.add(q)
    return q


def unsubscribe(q):
    with _lock:
        _subs.discard(q)


def publish(kind, data=None):
    item = (kind, data or {}, time.time())
    with _lock:
        subs = list(_subs)
    for q in subs:
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


def listeners():
    with _lock:
        return len(_subs)
