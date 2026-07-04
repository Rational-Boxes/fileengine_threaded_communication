"""Primary/replica failover primitives (mirrors CSAI's failover.py).

A lazy circuit-breaker: a failed primary connection trips it for a cooldown, during
which reads fall back to the replica; after the cooldown the next operation re-probes
the primary and resumes on success. No background threads.
"""
from __future__ import annotations

import time
from typing import Callable


class DegradedReadOnly(RuntimeError):
    """A write was attempted while the primary is unavailable and the service is in
    read-only fallback mode."""


class CircuitBreaker:
    """Tracks primary availability with a cooldown. ``clock`` is injectable so the
    state transitions are deterministically testable."""

    def __init__(self, cooldown_s: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.cooldown_s = float(cooldown_s)
        self._clock = clock
        self._down_until = 0.0

    def should_try_primary(self) -> bool:
        return self._clock() >= self._down_until

    def is_degraded(self) -> bool:
        return self._clock() < self._down_until

    def trip(self) -> None:
        self._down_until = self._clock() + self.cooldown_s

    def reset(self) -> None:
        self._down_until = 0.0
