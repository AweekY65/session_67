"""Simulated lock clients.

``LeaseClient`` is a plain synchronous handle. ``AutoRenewClient`` runs a
background thread (the goroutine analogue) that keeps its lease alive
until paused, stopped or fenced out. All timing goes through the injected
clock, so tests drive it with ``FakeClock.advance`` instead of sleeping.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from .clock import Clock
from .lock import Lease, LeaseLock


class ClientPaused(Exception):
    pass


class LeaseClient:
    """A client that can be frozen with ``pause()`` to simulate a stall
    (GC pause, network partition, ...). While paused every operation
    raises :class:`ClientPaused`."""

    def __init__(self, lock: LeaseLock, client_id: str) -> None:
        self._lock = lock
        self.client_id = client_id
        self._paused = threading.Event()  # set == paused
        self.lease: Optional[Lease] = None

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def _check(self) -> None:
        if self._paused.is_set():
            raise ClientPaused(self.client_id)

    def acquire(self, resource: str, ttl: float) -> Optional[Lease]:
        self._check()
        self.lease = self._lock.acquire(resource, self.client_id, ttl)
        return self.lease

    def renew(self, ttl: float) -> Optional[Lease]:
        self._check()
        if self.lease is None:
            return None
        renewed = self._lock.renew(
            self.lease.resource, self.client_id, self.lease.token, ttl
        )
        if renewed is None:
            self.lease = None
            return None
        self.lease = renewed
        return self.lease

    def release(self) -> bool:
        self._check()
        if self.lease is None:
            return False
        ok = self._lock.release(
            self.lease.resource, self.client_id, self.lease.token
        )
        if ok:
            self.lease = None
        return ok


class AutoRenewClient:
    """Background-thread client that acquires a resource and keeps the
    lease renewed. ``pause()`` freezes the thread (simulated partition);
    while frozen the lease naturally expires and another client can take
    over. Events are reported through ``on_event`` so tests can
    synchronize without sleeping.

    Events: "acquired", "renewed", "lost", "stopped".
    """

    def __init__(
        self,
        lock: LeaseLock,
        clock: Clock,
        client_id: str,
        resource: str,
        ttl: float,
        on_event: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._lock = lock
        self._clock = clock
        self.client_id = client_id
        self._resource = resource
        self._ttl = ttl
        self._on_event = on_event or (lambda event: None)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._state_mu = threading.Lock()
        self.lease: Optional[Lease] = None
        self._thread = threading.Thread(
            target=self._run, name=f"lease-client-{client_id}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def pause(self) -> None:
        self._paused.set()
        self._wake()

    def resume(self) -> None:
        self._paused.clear()
        self._wake()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake()
        self._thread.join(timeout=timeout)

    @property
    def current_lease(self) -> Optional[Lease]:
        with self._state_mu:
            return self.lease

    def _wake(self) -> None:
        poke = getattr(self._clock, "poke", None)
        if poke is not None:
            poke()

    def _aborted(self) -> bool:
        return self._stop.is_set() or self._paused.is_set()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._paused.is_set():
                    self._clock.wait_until(float("inf"), abort=lambda: not self._paused.is_set() or self._stop.is_set())
                    continue
                with self._state_mu:
                    lease = self.lease
                if lease is None:
                    acquired = self._lock.acquire(
                        self._resource, self.client_id, self._ttl
                    )
                    if acquired is not None:
                        with self._state_mu:
                            self.lease = acquired
                        self._on_event("acquired")
                        continue
                    # Someone else holds it; retry shortly.
                    self._clock.wait_until(self._clock.now() + self._ttl / 4, abort=self._aborted)
                    continue
                # Sleep until halfway to expiry, then renew.
                renew_at = lease.expires_at - self._ttl / 2
                if not self._clock.wait_until(renew_at, abort=self._aborted):
                    continue
                with self._state_mu:
                    lease = self.lease
                if lease is None:
                    continue
                renewed = self._lock.renew(
                    self._resource, self.client_id, lease.token, self._ttl
                )
                with self._state_mu:
                    self.lease = renewed
                if renewed is None:
                    self._on_event("lost")
                else:
                    self._on_event("renewed")
        finally:
            self._on_event("stopped")
