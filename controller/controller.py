"""Desk Controller: die deterministische Verwaltungsebene.

- nimmt Befehle wie light.set_brightness(60) an
- prüft sie gegen die Fähigkeiten des Managers (kein Wrap-Around)
- routet sie an den richtigen Manager
- trägt den tatsächlichen Zustand in den DESK_STATE ein
- reagiert auf Events nach festen technischen Regeln (z. B. Taster -> Licht an/aus)

Intelligente Entscheidungen trifft hier niemand – das macht später Jarvis.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .desk_state import DeskState
from .manager_link import ManagerLink
from .protocol import DeskError, ErrorCode, Status, validate_params

log = logging.getLogger(__name__)

# Diese Aktionen führt jeder Manager auch im Not-Aus aus (sonst kommt man nie heraus).
ALWAYS_ALLOWED_ACTIONS = {"reset"}


class DeskController:
    def __init__(self, desk_state: Optional[DeskState] = None):
        self.desk_state = desk_state or DeskState()
        self.links: dict[str, ManagerLink] = {}
        self.events: list[dict] = []  # letzte Events, für Anzeige/Diagnose
        self.max_events = 100

    # ------------------------------------------------------------ Aufbau

    def add_link(self, link: ManagerLink) -> None:
        link.on_event = self._on_event
        self.links[link.name] = link

    def start(self) -> None:
        for link in self.links.values():
            link.start()

    def stop(self) -> None:
        for link in self.links.values():
            link.stop()

    # ------------------------------------------------------------ Befehle

    def execute(self, manager: str, action: str, params: Optional[dict] = None) -> dict:
        """Einen Befehl ausführen. Gibt den tatsächlichen Zustand zurück oder wirft DeskError."""
        params = dict(params or {})
        link = self._link(manager)

        if action.startswith("adjust_"):
            return self._adjust(link, action, params)

        info = self.desk_state.manager(manager)
        self._check_reachable(manager, info.status, action)

        actions = info.capabilities.get("actions", {})
        if action not in actions:
            raise DeskError(ErrorCode.UNKNOWN_ACTION, f"{manager} kann '{action}' nicht",
                            {"available": sorted(actions)})
        validate_params(actions[action].get("params", {}), params)

        try:
            data = link.command(action, params)
        except DeskError as exc:
            self._remember_error(manager, exc)
            if exc.code == ErrorCode.TIMEOUT:
                # Nicht blind wiederholen: lieber nachfragen, was wirklich passiert ist.
                self._refresh_state(link)
            raise

        result = self._apply_result(manager, action, data)
        self.desk_state.set_error(manager, None)
        return result

    def _apply_result(self, manager: str, action: str, data: Any) -> dict:
        if action == "reset":
            # Antwort auf reset: {"status": ..., "state": {...}}
            data = data or {}
            if "state" in data:
                self.desk_state.set_state(manager, data["state"])
            if "status" in data:
                self.desk_state.set_status(manager, data["status"])
            return self.desk_state.manager(manager).state
        if isinstance(data, dict):
            self.desk_state.set_state(manager, data)
        return self.desk_state.manager(manager).state

    def _adjust(self, link: ManagerLink, action: str, params: dict) -> dict:
        """Relative Änderung, z. B. adjust_brightness(delta=-10).

        Grenzen werden nicht umgebrochen: Ist keine Änderung möglich, gibt es einen
        Fehler. Sonst wird die größte mögliche Änderung ausgeführt.
        """
        key = action[len("adjust_"):]
        base_action = f"set_{key}"
        info = self.desk_state.manager(link.name)
        spec = info.capabilities.get("actions", {}).get(base_action, {}).get("params", {}).get("value")
        if not isinstance(spec, dict) or spec.get("type") != "int" or key not in info.state:
            raise DeskError(ErrorCode.UNKNOWN_ACTION, f"{link.name} kann '{action}' nicht")

        delta = params.get("delta")
        if set(params) != {"delta"} or not isinstance(delta, int) or isinstance(delta, bool):
            raise DeskError(ErrorCode.INVALID_PARAMS, "erwartet genau einen Parameter 'delta' (ganze Zahl)")

        current = info.state[key]
        lo, hi = spec.get("min"), spec.get("max")
        target = current + delta
        if lo is not None:
            target = max(lo, target)
        if hi is not None:
            target = min(hi, target)
        if target == current:
            raise DeskError(ErrorCode.NO_CHANGE_POSSIBLE, f"{key} ist bereits {current}",
                            {"current": current, "min": lo, "max": hi})
        return self.execute(link.name, base_action, {"value": target})

    def query(self, manager: str, target: str = "state") -> Any:
        """Frischen Wert direkt beim Manager abfragen."""
        link = self._link(manager)
        data = link.query(target)
        if target == "state" and isinstance(data, dict):
            self.desk_state.set_state(manager, data)
        return data

    def reset(self, manager: str) -> dict:
        return self.execute(manager, "reset")

    # ------------------------------------------------------------ Events

    def _on_event(self, manager: str, msg: dict) -> None:
        """Feste technische Regeln. Läuft im Event-Thread des Managers."""
        event, data = msg["event"], msg.get("data", {})
        self.events.append({"manager": manager, "event": event, "data": data})
        del self.events[:-self.max_events]
        log.info("EVENT %s.%s %s", manager, event, data or "")

        if event == "STATE_CHANGED":
            self.desk_state.set_state(manager, data)
        elif event == "ESTOP_ACTIVE":
            self.desk_state.set_status(manager, Status.EMERGENCY)
            if "state" in data:
                self.desk_state.set_state(manager, data["state"])
        elif event == "ESTOP_RELEASED":
            # Bleibt EMERGENCY. Weiter geht es nur mit einem bewussten reset.
            log.info("%s: Not-Aus gelöst – für Neustart 'reset' ausführen", manager)
        elif event == "BUTTON_PRESSED":
            self._on_button(manager, data.get("button"))

    def _on_button(self, manager: str, button: Optional[str]) -> None:
        if button == "light":
            state = self.desk_state.manager(manager).state
            try:
                self.execute(manager, "set_power", {"on": not state.get("power", False)})
            except DeskError as exc:
                log.warning("Taster %s.%s ignoriert: %s", manager, button, exc)
        else:
            log.info("Taster %s.%s hat keine Funktion", manager, button)

    # ------------------------------------------------------------ Hilfen

    def _link(self, manager: str) -> ManagerLink:
        link = self.links.get(manager)
        if link is None:
            raise DeskError(ErrorCode.UNKNOWN_MANAGER, f"kein Manager '{manager}'",
                            {"available": sorted(self.links)})
        return link

    @staticmethod
    def _check_reachable(manager: str, status: str, action: str) -> None:
        if status == Status.OFFLINE:
            raise DeskError(ErrorCode.NOT_CONNECTED, f"{manager} ist nicht erreichbar")
        if status == Status.STARTING:
            raise DeskError(ErrorCode.NOT_READY, f"{manager} startet noch")
        if status == Status.EMERGENCY and action not in ALWAYS_ALLOWED_ACTIONS:
            raise DeskError(ErrorCode.ESTOP_ACTIVE, f"{manager}: Not-Aus aktiv")

    def _remember_error(self, manager: str, exc: DeskError) -> None:
        self.desk_state.set_error(manager, exc.to_dict())

    def _refresh_state(self, link: ManagerLink) -> None:
        try:
            self.query(link.name, "state")
        except DeskError:
            pass
