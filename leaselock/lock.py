"""Core lease lock: acquire / renew / release with fencing tokens.

Guarantees:
  * At most one *valid* (unexpired) lease per resource at any moment.
  * Every successful acquire hands out a strictly increasing fencing
    token (monotonic across expiry, release and process restarts).
  * An expired lease can never be renewed.
  * On startup, leases whose expiry is already in the past (according to
    the injected clock) are discarded, never resurrected.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Dict, Optional

from .clock import Clock
from .store import StateStore


@dataclass(frozen=True)
class Lease:
    resource: str
    holder: str
    token: int
    expires_at: float


class LeaseLock:
    def __init__(
        self,
        store: StateStore,
        clock: Clock,
        log_path: Optional[str] = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._mu = threading.Lock()
        self._log_path = log_path
        self._log_mu = threading.Lock()
        state = self._store.load()
        self._last_token: int = int(state.get("last_token", 0))
        self._leases: Dict[str, dict] = dict(state.get("leases", {}))
        # Recovery rule: never resurrect an already-expired lease.
        now = self._clock.now()
        expired = [r for r, l in self._leases.items() if l["expires_at"] <= now]
        for resource in expired:
            self._log("expire_on_recovery", resource, self._leases[resource])
            del self._leases[resource]
        if expired:
            self._persist()

    # ------------------------------------------------------------------ API

    def acquire(self, resource: str, holder: str, ttl: float) -> Optional[Lease]:
        """Try to acquire ``resource``. Returns the Lease or None."""
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        with self._mu:
            current = self._leases.get(resource)
            now = self._clock.now()
            if current is not None and current["expires_at"] > now:
                self._log("acquire_denied", resource, {"holder": holder})
                return None
            self._last_token += 1
            record = {
                "holder": holder,
                "token": self._last_token,
                "expires_at": now + ttl,
            }
            self._leases[resource] = record
            self._persist()
            self._log("acquire", resource, record)
            return Lease(resource, holder, record["token"], record["expires_at"])

    def renew(self, resource: str, holder: str, token: int, ttl: float) -> Optional[Lease]:
        """Extend the lease. Fails for expired leases and non-holders."""
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        with self._mu:
            current = self._leases.get(resource)
            now = self._clock.now()
            if (
                current is None
                or current["holder"] != holder
                or current["token"] != token
                or current["expires_at"] <= now
            ):
                self._log(
                    "renew_denied",
                    resource,
                    {"holder": holder, "token": token},
                )
                return None
            current["expires_at"] = now + ttl
            self._persist()
            self._log("renew", resource, current)
            return Lease(resource, holder, current["token"], current["expires_at"])

    def release(self, resource: str, holder: str, token: int) -> bool:
        """Release the lease. Only the current holder+token may release."""
        with self._mu:
            current = self._leases.get(resource)
            now = self._clock.now()
            if (
                current is None
                or current["holder"] != holder
                or current["token"] != token
                or current["expires_at"] <= now
            ):
                self._log(
                    "release_denied",
                    resource,
                    {"holder": holder, "token": token},
                )
                return False
            del self._leases[resource]
            self._persist()
            self._log("release", resource, current)
            return True

    def current_lease(self, resource: str) -> Optional[Lease]:
        """The currently valid lease for ``resource``, if any."""
        with self._mu:
            current = self._leases.get(resource)
            if current is None or current["expires_at"] <= self._clock.now():
                return None
            return Lease(
                resource,
                current["holder"],
                current["token"],
                current["expires_at"],
            )

    @property
    def last_token(self) -> int:
        with self._mu:
            return self._last_token

    # -------------------------------------------------------------- intern

    def _persist(self) -> None:
        self._store.save(
            {
                "last_token": self._last_token,
                "leases": self._leases,
            }
        )

    def _log(self, event: str, resource: str, detail: dict) -> None:
        if self._log_path is None:
            return
        entry = {
            "ts": self._clock.now(),
            "event": event,
            "resource": resource,
            "detail": dict(detail),
        }
        line = json.dumps(entry, sort_keys=True)
        with self._log_mu:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
