"""Simulierter LightManager – hält sich genau an docs/DESIGN_PROTOCOL.md.

Wird für die Tests und für `--simulate` benutzt. Er ist gleichzeitig die
Referenz für die ESP32-Firmware: was hier passiert, muss die Firmware genauso tun.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from . import protocol as p
from .protocol import ErrorCode, MsgType, ProtocolError, Status
from .transport import Transport, TransportError

log = logging.getLogger(__name__)

FIRMWARE_VERSION = "0.1.0-sim"

CAPABILITIES = {
    "actions": {
        "set_power": {"params": {"on": "bool"}},
        "set_brightness": {"params": {"value": {"type": "int", "min": 0, "max": 100}}},
        "reset": {"params": {}},
    },
    "events": ["BOOT", "STATE_CHANGED", "BUTTON_PRESSED", "ESTOP_ACTIVE", "ESTOP_RELEASED"],
}


class SimulatedLightManager:
    def __init__(self, transport: Transport, controller_timeout: float = 3.0, uid: str = "sim-0001"):
        self.transport = transport
        self.controller_timeout = controller_timeout
        self.uid = uid

        self._lock = threading.Lock()
        self.power = False
        self.brightness = 50
        self.status = Status.STARTING
        self.estop_pressed = False

        self._seq = 0
        self._last_contact = time.monotonic()
        self._stop = threading.Event()
        self._watchdog: Optional[threading.Thread] = None

        self.frozen = False          # True = antwortet nicht mehr (Absturz simulieren)
        self.commands_received = 0   # für Tests: wie viele COMMANDs kamen an

    # ------------------------------------------------------------ Start / Stopp

    def start(self) -> None:
        self.transport.set_line_handler(self._on_line)
        self.transport.open()
        self._stop.clear()
        self._watchdog = threading.Thread(target=self._watch_controller, name="sim-light-watchdog", daemon=True)
        self._watchdog.start()
        self.boot()

    def stop(self) -> None:
        self._stop.set()
        if self._watchdog is not None:
            self._watchdog.join(timeout=2)
        self.transport.close()

    def boot(self) -> None:
        """Wie ein Neustart des ESP32: Zustand zurücksetzen, BOOT melden."""
        with self._lock:
            self.power = False
            self.brightness = 50
            self._seq = 0
            self.status = Status.EMERGENCY if self.estop_pressed else Status.READY
            self._last_contact = time.monotonic()
        self._send_event("BOOT", {"manager": "light", "firmware": FIRMWARE_VERSION, "protocol": p.PROTOCOL_VERSION})

    # ------------------------------------------------------------ "Hardware" bedienen

    def press_button(self) -> None:
        self._send_event("BUTTON_PRESSED", {"button": "light"})

    def set_estop(self, pressed: bool) -> None:
        with self._lock:
            if pressed == self.estop_pressed:
                return
            self.estop_pressed = pressed
            if pressed:
                # Lokale Sicherheitsreaktion: sofort aus, ohne auf den Controller zu warten.
                self.power = False
                self.status = Status.EMERGENCY
                state = self._state()
        if pressed:
            self._send_event("ESTOP_ACTIVE", {"state": state})
        else:
            self._send_event("ESTOP_RELEASED", {})

    # ------------------------------------------------------------ Protokoll

    def _state(self) -> dict:
        return {"power": self.power, "brightness": self.brightness}

    def _send(self, msg: dict) -> None:
        try:
            self.transport.send_line(p.encode(msg))
        except TransportError:
            pass

    def _send_event(self, event: str, data: dict) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
        self._send(p.make_event(seq, event, data))

    def _on_line(self, line: bytes) -> None:
        if self.frozen:
            return
        try:
            msg = p.decode(line, allowed_types=p.CONTROLLER_TYPES)
        except ProtocolError as exc:
            self._send(p.make_error(None, exc.code, exc.message))
            return

        with self._lock:
            self._last_contact = time.monotonic()
            if self.status == Status.CONTROLLER_LOST:
                self.status = Status.READY

        msg_type, msg_id = msg["type"], msg["id"]
        if msg_type == MsgType.PING:
            self._send(p.make_pong(msg_id, self.status))
        elif msg_type == MsgType.QUERY:
            self._handle_query(msg_id, msg["target"])
        elif msg_type == MsgType.COMMAND:
            self.commands_received += 1
            self._handle_command(msg_id, msg["action"], msg.get("params", {}))

    def _handle_query(self, msg_id: int, target: str) -> None:
        with self._lock:
            if target == "identity":
                data = {"manager": "light", "firmware": FIRMWARE_VERSION,
                        "protocol": p.PROTOCOL_VERSION, "uid": self.uid}
            elif target == "capabilities":
                data = CAPABILITIES
            elif target == "state":
                data = self._state()
            elif target == "status":
                data = {"status": self.status}
            else:
                data = None
        if data is None:
            self._send(p.make_error(msg_id, ErrorCode.UNKNOWN_ACTION, f"unbekanntes target '{target}'"))
        else:
            self._send(p.make_response(msg_id, data))

    def _handle_command(self, msg_id: int, action: str, params: dict) -> None:
        spec = CAPABILITIES["actions"].get(action)
        if spec is None:
            self._send(p.make_error(msg_id, ErrorCode.UNKNOWN_ACTION, f"unbekannte action '{action}'"))
            return
        try:
            p.validate_params(spec["params"], params)  # Manager prüft selbst noch einmal
        except p.DeskError as exc:
            self._send(p.make_error(msg_id, exc.code, exc.message, exc.details))
            return

        with self._lock:
            if action == "reset":
                if self.estop_pressed:
                    reply = p.make_error(msg_id, ErrorCode.ESTOP_ACTIVE, "Not-Aus ist noch gedrückt")
                else:
                    # Nach einem Reset bleibt das Licht aus – kein blinder Wiederanlauf.
                    self.power = False
                    self.status = Status.READY
                    reply = p.make_response(msg_id, {"status": self.status, "state": self._state()})
            elif self.status == Status.EMERGENCY:
                reply = p.make_error(msg_id, ErrorCode.ESTOP_ACTIVE, "Not-Aus aktiv")
            elif self.status != Status.READY:
                reply = p.make_error(msg_id, ErrorCode.NOT_READY, f"Status {self.status}")
            elif action == "set_power":
                self.power = params["on"]
                reply = p.make_response(msg_id, self._state())
            else:  # set_brightness
                self.brightness = params["value"]
                reply = p.make_response(msg_id, self._state())
        self._send(reply)

    def _watch_controller(self) -> None:
        while not self._stop.wait(0.05):
            with self._lock:
                silent = time.monotonic() - self._last_contact
                if silent > self.controller_timeout and self.status == Status.READY:
                    # Sicheren Zustand halten, Status melden (Status-LED später gelb blinkend).
                    self.status = Status.CONTROLLER_LOST
                    log.info("sim-light: Controller verloren")
