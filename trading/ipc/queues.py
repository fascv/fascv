from __future__ import annotations

from multiprocessing.queues import Queue
from queue import Empty, Full
from typing import Any, Optional


def try_put(queue: Queue, item: Any, block: bool = False, timeout: float = 0.0) -> bool:
    try:
        queue.put(item, block=block, timeout=timeout)
        return True
    except Full:
        return False


def put_latest(queue: Queue, item: Any) -> bool:
    try:
        queue.put_nowait(item)
        return True
    except Full:
        try:
            queue.get_nowait()
        except Empty:
            pass
        try:
            queue.put_nowait(item)
            return True
        except Full:
            return False


def try_get(queue: Queue) -> Optional[Any]:
    try:
        return queue.get_nowait()
    except Empty:
        return None


def queue_depth(queue: Queue) -> int:
    try:
        return queue.qsize()
    except Exception:
        return -1
