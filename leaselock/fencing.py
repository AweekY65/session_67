"""A fenced resource: the downstream system that checks fencing tokens.

This simulates e.g. a file or database that only accepts writes carrying
a fencing token greater than every token it has seen before. A stale
holder that wakes up after losing its lease is rejected here even though
it *believes* it still owns the lock.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Optional


class StaleFencingToken(Exception):
    """Raised when a write carries a token older than the newest seen."""


class FencedStore:
    """Local key/value store (memory or JSON file) enforcing token order."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path
        self._mu = threading.Lock()
        self._data: Dict[str, Any] = {}
        self._max_token = 0
        if path is not None and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            self._data = saved.get("data", {})
            self._max_token = int(saved.get("max_token", 0))

    @property
    def max_token(self) -> int:
        with self._mu:
            return self._max_token

    def write(self, key: str, value: Any, token: int) -> None:
        with self._mu:
            if token <= self._max_token:
                raise StaleFencingToken(
                    f"token {token} <= last accepted token {self._max_token}"
                )
            self._max_token = token
            self._data[key] = value
            self._persist()

    def read(self, key: str, default: Any = None) -> Any:
        with self._mu:
            return self._data.get(key, default)

    def _persist(self) -> None:
        if self._path is None:
            return
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"max_token": self._max_token, "data": self._data}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)
