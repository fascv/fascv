from __future__ import annotations

from collections import deque
from typing import Deque, Iterable, Optional


class RollingWindow:
    def __init__(self, size: int):
        if size <= 0:
            raise ValueError("size must be positive")
        self.size = size
        self._values: Deque[float] = deque(maxlen=size)

    def push(self, value: float) -> None:
        self._values.append(value)

    def values(self) -> Iterable[float]:
        return list(self._values)

    def mean(self) -> Optional[float]:
        if not self._values:
            return None
        return sum(self._values) / len(self._values)

    def std(self) -> Optional[float]:
        n = len(self._values)
        if n < 2:
            return None
        m = self.mean()
        var = sum((x - m) ** 2 for x in self._values) / (n - 1)
        return var ** 0.5

    def last(self) -> Optional[float]:
        return self._values[-1] if self._values else None

    def full(self) -> bool:
        return len(self._values) == self.size
