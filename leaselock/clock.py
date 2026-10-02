"""Injectable clocks.

All time logic in the lease lock goes through the ``Clock`` protocol, so
tests can use :class:`FakeClock` and never depend on real sleeps.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional, Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Current time in seconds (monotonic semantics)."""
        ...

    def wait_until(self, deadline: float, abort: Optional[Callable[[], bool]] = None) -> bool:
        """Block until clock reaches ``deadline`` or ``abort()`` is true.

        Returns True if the deadline was reached.
        """
        ...


class SystemClock:
    """Wall-clock implementation backed by ``time.monotonic``."""

    def __init__(self) -> None:
        self._cond = threading.Condition()

    def now(self) -> float:
        return time.monotonic()

    def wait_until(self, deadline: float, abort: Optional[Callable[[], bool]] = None) -> bool:
        with self._cond:
            while True:
                if abort is not None and abort():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True
                self._cond.wait(timeout=remaining)

    def poke(self) -> None:
        with self._cond:
            self._cond.notify_all()


class FakeClock:
    """Manually advanced clock for deterministic tests.

    Threads can block in :meth:`wait_until`; ``advance`` wakes them up.
    ``poke`` wakes waiters without moving time (used to propagate aborts).
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)
        self._cond = threading.Condition()

    def now(self) -> float:
        with self._cond:
            return self._t

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("cannot move clock backwards")
        with self._cond:
            self._t += seconds
            self._cond.notify_all()
            return self._t

    def poke(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def wait_until(self, deadline: float, abort: Optional[Callable[[], bool]] = None) -> bool:
        with self._cond:
            while self._t < deadline:
                if abort is not None and abort():
                    return False
                self._cond.wait()
            return True
