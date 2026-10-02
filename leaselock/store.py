"""Local-only persistence for lock state.

State lives either in memory or in a single JSON file on the local
filesystem. Writes are atomic (temp file + os.replace) so a crash cannot
leave a half-written state file behind.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Protocol


class StateStore(Protocol):
    def load(self) -> Dict[str, Any]: ...
    def save(self, state: Dict[str, Any]) -> None: ...


class MemoryStore:
    """Volatile store; nothing survives a restart."""

    def __init__(self) -> None:
        self._state: Dict[str, Any] = {}
        self._mu = threading.Lock()

    def load(self) -> Dict[str, Any]:
        with self._mu:
            return json.loads(json.dumps(self._state))

    def save(self, state: Dict[str, Any]) -> None:
        with self._mu:
            self._state = json.loads(json.dumps(state))


class FileStore:
    """JSON-file store with atomic replacement."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._mu = threading.Lock()

    def load(self) -> Dict[str, Any]:
        with self._mu:
            if not os.path.exists(self._path):
                return {}
            with open(self._path, "r", encoding="utf-8") as fh:
                return json.load(fh)

    def save(self, state: Dict[str, Any]) -> None:
        with self._mu:
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
