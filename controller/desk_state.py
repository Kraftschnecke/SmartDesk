"""DESK_STATE: der zentrale Zustand des Tisches.

Hier steht, wie der Tisch *tatsächlich* ist: welche Manager da sind, ihr Status,
was sie können und ihr aktueller Zustand (immer aus der Rückmeldung des
Managers, nie aus der Anfrage). Thread-sicher.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .protocol import Status


@dataclass
class ManagerInfo:
    name: str
    status: str = Status.OFFLINE
    identity: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    last_error: Optional[dict] = None
    last_seen: Optional[float] = None  # time.time() der letzten Nachricht


StatusListener = Callable[[str, str, str], None]  # (manager, alt, neu)


class DeskState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._managers: dict[str, ManagerInfo] = {}
        self._status_listeners: list[StatusListener] = []
        self.started_at = time.time()

    # ------------------------------------------------------------ Lesen

    def has_manager(self, name: str) -> bool:
        with self._lock:
            return name in self._managers

    def manager(self, name: str) -> ManagerInfo:
        """Kopie der Infos eines Managers (KeyError, wenn unbekannt)."""
        with self._lock:
            return copy.deepcopy(self._managers[name])

    def snapshot(self) -> dict[str, Any]:
        """Kompletter Zustand als dict (Kopie)."""
        with self._lock:
            return {
                "started_at": self.started_at,
                "emergency": any(m.status == Status.EMERGENCY for m in self._managers.values()),
                "managers": {
                    name: {
                        "status": m.status,
                        "identity": copy.deepcopy(m.identity),
                        "state": copy.deepcopy(m.state),
                        "last_error": copy.deepcopy(m.last_error),
                        "last_seen": m.last_seen,
                    }
                    for name, m in self._managers.items()
                },
            }

    # ------------------------------------------------------------ Schreiben

    def add_manager(self, name: str) -> None:
        with self._lock:
            self._managers.setdefault(name, ManagerInfo(name))

    def on_status_change(self, listener: StatusListener) -> None:
        self._status_listeners.append(listener)

    def set_status(self, name: str, status: str) -> None:
        with self._lock:
            info = self._managers[name]
            old = info.status
            info.status = status
        if old != status:
            for listener in list(self._status_listeners):
                listener(name, old, status)

    def set_identity(self, name: str, identity: dict) -> None:
        with self._lock:
            self._managers[name].identity = copy.deepcopy(identity)

    def set_capabilities(self, name: str, capabilities: dict) -> None:
        with self._lock:
            self._managers[name].capabilities = copy.deepcopy(capabilities)

    def set_state(self, name: str, state: dict) -> None:
        with self._lock:
            self._managers[name].state = copy.deepcopy(state)

    def set_error(self, name: str, error: Optional[dict]) -> None:
        with self._lock:
            self._managers[name].last_error = copy.deepcopy(error)

    def touch(self, name: str) -> None:
        with self._lock:
            self._managers[name].last_seen = time.time()
