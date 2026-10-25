"""Verbindung zu genau einem Manager.

Aufgaben:
- Anfragen senden und auf die passende Antwort warten (id, Timeout)
- Heartbeat: jede Sekunde PING, nach 3 verpassten PONGs -> OFFLINE
- Handshake nach BOOT oder Wiederverbindung: identity, capabilities, state, status
- Events an den Controller weiterreichen (in einem eigenen Thread, damit der
  Controller darauf wieder Anfragen senden kann)

Die Link-Klasse entscheidet nichts. Sie meldet nur, was passiert.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import protocol as p
from .desk_state import DeskState
from .protocol import DeskError, ErrorCode, MsgType, ProtocolError, Status
from .transport import Transport, TransportError

log = logging.getLogger(__name__)

EventHandler = Callable[[str, dict], None]  # (manager, event-Nachricht)


@dataclass
class _Pending:
    done: threading.Event = field(default_factory=threading.Event)
    reply: Optional[dict] = None


class ManagerLink:
    def __init__(
        self,
        name: str,
        transport: Transport,
        desk_state: DeskState,
        on_event: Optional[EventHandler] = None,
        request_timeout: float = p.REQUEST_TIMEOUT_S,
        ping_interval: float = p.PING_INTERVAL_S,
        missed_pings_offline: int = p.MISSED_PINGS_OFFLINE,
    ):
        self.name = name
        self.transport = transport
        self.desk_state = desk_state
        self.on_event = on_event
        self.request_timeout = request_timeout
        self.ping_interval = ping_interval
        self.missed_pings_offline = missed_pings_offline

        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._missed = 0
        self._ready = False  # Handshake abgeschlossen
        self._handshake_lock = threading.Lock()
        self._last_seq: Optional[int] = None

        self._events: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        self.lost_events = 0
        self.bad_lines = 0

        desk_state.add_manager(name)

    # ------------------------------------------------------------ Start / Stopp

    def start(self) -> None:
        self.transport.set_line_handler(self._on_line)
        self.transport.open()
        for target, label in ((self._dispatch_loop, "events"), (self._heartbeat_loop, "heartbeat")):
            t = threading.Thread(target=target, name=f"{self.name}-{label}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._events.put(None)
        for t in self._threads:
            t.join(timeout=2)
        self._threads.clear()
        self.transport.close()

    @property
    def is_ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------ Anfragen

    def command(self, action: str, params: Optional[dict] = None) -> Any:
        return self._request(lambda i: p.make_command(i, action, params))["data"]

    def query(self, target: str) -> Any:
        return self._request(lambda i: p.make_query(i, target))["data"]

    def ping(self) -> str:
        return self._request(p.make_ping)["status"]

    def _request(self, build: Callable[[int], dict]) -> dict:
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            pending = _Pending()
            self._pending[msg_id] = pending
        try:
            try:
                self.transport.send_line(p.encode(build(msg_id)))
            except TransportError as exc:
                raise DeskError(ErrorCode.NOT_CONNECTED, str(exc)) from None
            if not pending.done.wait(self.request_timeout):
                raise DeskError(ErrorCode.TIMEOUT, f"{self.name}: keine Antwort in {self.request_timeout}s")
        finally:
            with self._lock:
                self._pending.pop(msg_id, None)

        reply = pending.reply or {}
        if reply.get("type") == MsgType.ERROR:
            raise DeskError(reply.get("code", "UNKNOWN"), reply.get("message", ""), reply.get("details"))
        if reply.get("type") == MsgType.RESPONSE and "data" not in reply:
            reply["data"] = None
        return reply

    # ------------------------------------------------------------ Empfang

    def _on_line(self, line: bytes) -> None:
        """Läuft im Lese-Thread des Transports. Darf nie blockieren."""
        try:
            msg = p.decode(line, allowed_types=p.MANAGER_TYPES)
        except ProtocolError as exc:
            self.bad_lines += 1
            log.warning("%s: ungültige Zeile verworfen (%s)", self.name, exc)
            return

        self.desk_state.touch(self.name)
        msg_type = msg["type"]

        if msg_type in (MsgType.RESPONSE, MsgType.PONG) or (
            msg_type == MsgType.ERROR and msg.get("id") is not None
        ):
            with self._lock:
                pending = self._pending.get(msg["id"])
            if pending is None:
                log.debug("%s: verspätete Antwort id=%s verworfen", self.name, msg.get("id"))
                return
            pending.reply = msg
            pending.done.set()
            return

        # EVENT oder ERROR ohne id -> an den Event-Thread
        self._events.put(msg)

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            msg = self._events.get()
            if msg is None:
                return
            try:
                self._handle_async(msg)
            except Exception:
                log.exception("%s: Fehler bei der Event-Verarbeitung", self.name)

    def _handle_async(self, msg: dict) -> None:
        if msg["type"] == MsgType.ERROR:
            log.warning("%s meldet Fehler: %s %s", self.name, msg.get("code"), msg.get("message", ""))
            self.desk_state.set_error(self.name, {"code": msg.get("code"), "message": msg.get("message", "")})
            return

        event = msg["event"]
        seq = msg["seq"]
        if event == "BOOT":
            self._last_seq = seq
        elif self._last_seq is not None and seq != self._last_seq + 1:
            lost = max(0, seq - self._last_seq - 1)
            self.lost_events += lost
            log.warning("%s: %d Event(s) verloren (seq %d -> %d)", self.name, lost, self._last_seq, seq)
        self._last_seq = seq

        if event == "BOOT":
            log.info("%s: Manager gestartet (%s)", self.name, msg.get("data", {}))
            self._ready = False
            self.desk_state.set_status(self.name, Status.STARTING)
            self.handshake()

        if self.on_event is not None:
            self.on_event(self.name, msg)

    # ------------------------------------------------------------ Handshake / Heartbeat

    def handshake(self) -> bool:
        """Identität, Fähigkeiten, Zustand und Status neu abfragen. Keine alten Befehle senden."""
        with self._handshake_lock:
            try:
                identity = self.query("identity")
                capabilities = self.query("capabilities")
                state = self.query("state")
                status = self.query("status")
            except DeskError as exc:
                log.warning("%s: Handshake fehlgeschlagen (%s)", self.name, exc)
                self._ready = False
                return False
            self.desk_state.set_identity(self.name, identity or {})
            self.desk_state.set_capabilities(self.name, capabilities or {})
            self.desk_state.set_state(self.name, state or {})
            self.desk_state.set_status(self.name, (status or {}).get("status", Status.READY))
            self._ready = True
            log.info("%s: bereit (%s)", self.name, identity)
            return True

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.ping_interval):
            try:
                status = self.ping()
            except DeskError:
                self._missed += 1
                if self._missed >= self.missed_pings_offline:
                    if self.desk_state.manager(self.name).status != Status.OFFLINE:
                        log.warning("%s: keine Antwort mehr -> OFFLINE", self.name)
                        self.desk_state.set_status(self.name, Status.OFFLINE)
                    self._ready = False
                continue
            self._missed = 0
            if not self._ready:
                self.handshake()
            else:
                self.desk_state.set_status(self.name, status)
