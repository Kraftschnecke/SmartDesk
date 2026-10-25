"""Verbindungen zu einem Manager.

Ein Transport liefert empfangene Zeilen an einen Handler und kann Zeilen
senden. Er weiß nichts über JSON oder das Protokoll.

- SerialTransport: echte USB-Seriell-Verbindung (pyserial), verbindet sich
  nach einem Abbruch automatisch neu.
- MemoryTransport: zwei verbundene Enden im Speicher, für Tests und --simulate.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional

from .protocol import MAX_LINE_BYTES

log = logging.getLogger(__name__)

LineHandler = Callable[[bytes], None]


class TransportError(Exception):
    """Senden nicht möglich (z. B. keine Verbindung)."""


class Transport:
    def set_line_handler(self, handler: LineHandler) -> None:
        self._handler = handler

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def send_line(self, data: bytes) -> None:
        raise NotImplementedError

    def _deliver(self, line: bytes) -> None:
        handler = getattr(self, "_handler", None)
        if handler is None:
            return
        try:
            handler(line)
        except Exception:  # ein Fehler im Handler darf die Leseschleife nie beenden
            log.exception("Fehler beim Verarbeiten einer Zeile")


class LineSplitter:
    """Setzt Bytes zu Zeilen zusammen und verwirft zu lange Zeilen."""

    def __init__(self, max_len: int = MAX_LINE_BYTES):
        self._buf = bytearray()
        self._max = max_len
        self._discarding = False
        self.dropped = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        lines: list[bytes] = []
        for byte in chunk:
            if byte == 0x0A:  # \n
                if self._discarding:
                    self._discarding = False
                else:
                    lines.append(bytes(self._buf).rstrip(b"\r"))
                self._buf.clear()
                continue
            if self._discarding:
                continue
            self._buf.append(byte)
            if len(self._buf) > self._max:
                self._buf.clear()
                self._discarding = True
                self.dropped += 1
                log.warning("Zu lange Zeile verworfen")
        return lines


class SerialTransport(Transport):
    def __init__(self, port: str, baudrate: int = 115200, reconnect_delay: float = 2.0):
        self.port = port
        self.baudrate = baudrate
        self.reconnect_delay = reconnect_delay
        self._ser = None
        self._write_lock = threading.Lock()
        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def open(self) -> None:
        if self._thread is not None:
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, name=f"serial-{self.port}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._running.clear()
        self._close_port()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def send_line(self, data: bytes) -> None:
        with self._write_lock:
            ser = self._ser
            if ser is None:
                raise TransportError(f"{self.port}: nicht verbunden")
            try:
                ser.write(data)
            except Exception as exc:
                raise TransportError(f"{self.port}: Senden fehlgeschlagen ({exc})") from exc

    def _close_port(self) -> None:
        with self._write_lock:
            ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _run(self) -> None:
        import serial  # erst hier, damit Tests ohne pyserial laufen

        while self._running.is_set():
            try:
                ser = serial.Serial(self.port, self.baudrate, timeout=0.2)
            except (serial.SerialException, OSError) as exc:
                log.debug("%s: nicht erreichbar (%s)", self.port, exc)
                time.sleep(self.reconnect_delay)
                continue
            log.info("%s: verbunden", self.port)
            with self._write_lock:
                self._ser = ser
            splitter = LineSplitter()
            try:
                while self._running.is_set():
                    chunk = ser.read(256)
                    if chunk:
                        for line in splitter.feed(chunk):
                            self._deliver(line)
            except (serial.SerialException, OSError) as exc:
                log.warning("%s: Verbindung verloren (%s)", self.port, exc)
            finally:
                self._close_port()
            if self._running.is_set():
                time.sleep(self.reconnect_delay)


class MemoryTransport(Transport):
    """Ein Ende einer Verbindung im Speicher. Mit pair() zwei verbundene Enden bauen."""

    def __init__(self, name: str = "mem"):
        self.name = name
        self._peer: Optional[MemoryTransport] = None
        self._inbox: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._open = False
        self._thread: Optional[threading.Thread] = None
        self.cut = False  # True = Leitung "durchgeschnitten" (für Tests)

    @staticmethod
    def pair(name_a: str = "controller", name_b: str = "manager") -> tuple["MemoryTransport", "MemoryTransport"]:
        a, b = MemoryTransport(name_a), MemoryTransport(name_b)
        a._peer, b._peer = b, a
        return a, b

    def open(self) -> None:
        if self._open:
            return
        self._open = True
        self._thread = threading.Thread(target=self._run, name=f"mem-{self.name}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        self._inbox.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def send_line(self, data: bytes) -> None:
        peer = self._peer
        if not self._open or peer is None or not peer._open:
            raise TransportError(f"{self.name}: nicht verbunden")
        if self.cut or peer.cut:
            return  # Daten gehen verloren, wie bei einem abgezogenen Kabel
        splitter = LineSplitter()
        for line in splitter.feed(data):
            peer._inbox.put(line)

    def _run(self) -> None:
        while True:
            line = self._inbox.get()
            if line is None:
                return
            self._deliver(line)
