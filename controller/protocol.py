"""Protokoll Controller <-> Manager (siehe docs/DESIGN_PROTOCOL.md).

Eine JSON-Nachricht pro Zeile. Dieses Modul baut Nachrichten, wandelt sie in
Bytes um und prüft eingehende Zeilen. Es kennt keine Verbindung und keine
Hardware.
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 512

# Zeiten (Sekunden) – Standardwerte aus dem Protokoll
REQUEST_TIMEOUT_S = 0.5
PING_INTERVAL_S = 1.0
MISSED_PINGS_OFFLINE = 3


class MsgType:
    # Controller -> Manager
    COMMAND = "COMMAND"
    QUERY = "QUERY"
    PING = "PING"
    # Manager -> Controller
    RESPONSE = "RESPONSE"
    ERROR = "ERROR"
    EVENT = "EVENT"
    PONG = "PONG"


CONTROLLER_TYPES = {MsgType.COMMAND, MsgType.QUERY, MsgType.PING}
MANAGER_TYPES = {MsgType.RESPONSE, MsgType.ERROR, MsgType.EVENT, MsgType.PONG}
ALL_TYPES = CONTROLLER_TYPES | MANAGER_TYPES

# QUERY-Ziele, die jeder Manager kennen muss
STANDARD_QUERY_TARGETS = ("identity", "capabilities", "state", "status")


class ErrorCode:
    # aus dem Protokoll (Manager oder Controller)
    PARSE_ERROR = "PARSE_ERROR"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    INVALID_PARAMS = "INVALID_PARAMS"
    VALUE_OUT_OF_RANGE = "VALUE_OUT_OF_RANGE"
    NOT_READY = "NOT_READY"
    ESTOP_ACTIVE = "ESTOP_ACTIVE"
    HARDWARE_FAULT = "HARDWARE_FAULT"
    TIMEOUT = "TIMEOUT"
    # nur Controller-intern (gehen nie über die Leitung)
    UNKNOWN_MANAGER = "UNKNOWN_MANAGER"
    NOT_CONNECTED = "NOT_CONNECTED"
    NO_CHANGE_POSSIBLE = "NO_CHANGE_POSSIBLE"


class Status:
    STARTING = "STARTING"
    READY = "READY"
    WARNING = "WARNING"
    ERROR = "ERROR"
    EMERGENCY = "EMERGENCY"
    CONTROLLER_LOST = "CONTROLLER_LOST"
    # nur Controller-seitig: Manager antwortet nicht
    OFFLINE = "OFFLINE"


class ProtocolError(Exception):
    """Eine Zeile verletzt das Protokoll."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class DeskError(Exception):
    """Eine Anfrage konnte nicht ausgeführt werden (mit Fehlercode)."""

    def __init__(self, code: str, message: str = "", details: dict | None = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


# ---------------------------------------------------------------- Kodieren

def encode(msg: dict) -> bytes:
    """Nachricht -> eine Zeile Bytes (mit \\n). Zu lange Nachrichten sind ein Programmierfehler."""
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) + 1 > MAX_LINE_BYTES:
        raise ValueError(f"Nachricht zu lang ({len(data) + 1} > {MAX_LINE_BYTES} Byte)")
    return data + b"\n"


def decode(line: bytes, allowed_types: set[str] | None = None) -> dict:
    """Eine empfangene Zeile prüfen und als dict zurückgeben.

    Wirft ProtocolError mit passendem Code. Unbekannte Felder werden ignoriert.
    """
    line = line.rstrip(b"\r\n")
    if len(line) + 1 > MAX_LINE_BYTES:
        raise ProtocolError(ErrorCode.PARSE_ERROR, "Zeile zu lang")
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(ErrorCode.PARSE_ERROR, f"kein gültiges JSON ({exc})") from None
    if not isinstance(msg, dict):
        raise ProtocolError(ErrorCode.PARSE_ERROR, "Nachricht ist kein JSON-Objekt")

    if msg.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(ErrorCode.UNSUPPORTED_VERSION, f"Version {msg.get('v')!r}")

    msg_type = msg.get("type")
    types = allowed_types if allowed_types is not None else ALL_TYPES
    if msg_type not in types:
        raise ProtocolError(ErrorCode.UNKNOWN_TYPE, f"Typ {msg_type!r}")

    _check_fields(msg)
    return msg


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_fields(msg: dict) -> None:
    t = msg["type"]
    if t in (MsgType.COMMAND, MsgType.QUERY, MsgType.PING, MsgType.RESPONSE, MsgType.PONG):
        if not _is_int(msg.get("id")):
            raise ProtocolError(ErrorCode.PARSE_ERROR, f"{t} ohne gültige id")
    if t == MsgType.ERROR:
        if msg.get("id") is not None and not _is_int(msg.get("id")):
            raise ProtocolError(ErrorCode.PARSE_ERROR, "ERROR mit ungültiger id")
        if not isinstance(msg.get("code"), str):
            raise ProtocolError(ErrorCode.PARSE_ERROR, "ERROR ohne code")
    if t == MsgType.EVENT:
        if not _is_int(msg.get("seq")):
            raise ProtocolError(ErrorCode.PARSE_ERROR, "EVENT ohne seq")
        if not isinstance(msg.get("event"), str):
            raise ProtocolError(ErrorCode.PARSE_ERROR, "EVENT ohne Namen")
    if t == MsgType.COMMAND:
        if not isinstance(msg.get("action"), str):
            raise ProtocolError(ErrorCode.PARSE_ERROR, "COMMAND ohne action")
        if not isinstance(msg.get("params", {}), dict):
            raise ProtocolError(ErrorCode.PARSE_ERROR, "params ist kein Objekt")
    if t == MsgType.QUERY and not isinstance(msg.get("target"), str):
        raise ProtocolError(ErrorCode.PARSE_ERROR, "QUERY ohne target")


# ---------------------------------------------------------------- Bauen

def _base(msg_type: str, **fields: Any) -> dict:
    msg = {"v": PROTOCOL_VERSION, "type": msg_type}
    msg.update(fields)
    return msg


def make_command(msg_id: int, action: str, params: dict | None = None) -> dict:
    return _base(MsgType.COMMAND, id=msg_id, action=action, params=params or {})


def make_query(msg_id: int, target: str) -> dict:
    return _base(MsgType.QUERY, id=msg_id, target=target)


def make_ping(msg_id: int) -> dict:
    return _base(MsgType.PING, id=msg_id)


def make_response(msg_id: int, data: Any) -> dict:
    return _base(MsgType.RESPONSE, id=msg_id, data=data)


def make_error(msg_id: int | None, code: str, message: str = "", details: dict | None = None) -> dict:
    msg = _base(MsgType.ERROR, id=msg_id, code=code, message=message)
    if details:
        msg["details"] = details
    return msg


def make_event(seq: int, event: str, data: dict | None = None) -> dict:
    return _base(MsgType.EVENT, seq=seq, event=event, data=data or {})


def make_pong(msg_id: int, status: str) -> dict:
    return _base(MsgType.PONG, id=msg_id, status=status)


# ---------------------------------------------------------------- Parameter prüfen

def validate_params(spec: dict, params: dict) -> None:
    """Parameter gegen die Capabilities eines Managers prüfen.

    spec: {"value": {"type": "int", "min": 0, "max": 100}, "on": "bool"}
    Wirft DeskError (INVALID_PARAMS / VALUE_OUT_OF_RANGE). Kein Umbrechen, kein Kappen.
    """
    if not isinstance(params, dict):
        raise DeskError(ErrorCode.INVALID_PARAMS, "params ist kein Objekt")
    unknown = set(params) - set(spec)
    if unknown:
        raise DeskError(ErrorCode.INVALID_PARAMS, f"unbekannte Parameter: {sorted(unknown)}")
    for name, rule in spec.items():
        if name not in params:
            raise DeskError(ErrorCode.INVALID_PARAMS, f"Parameter '{name}' fehlt")
        _check_value(name, rule, params[name])


def _check_value(name: str, rule: Any, value: Any) -> None:
    kind = rule if isinstance(rule, str) else rule.get("type")
    if kind == "bool":
        if not isinstance(value, bool):
            raise DeskError(ErrorCode.INVALID_PARAMS, f"'{name}' muss true/false sein")
        return
    if kind == "int":
        if not _is_int(value):
            raise DeskError(ErrorCode.INVALID_PARAMS, f"'{name}' muss eine ganze Zahl sein")
        lo = rule.get("min") if isinstance(rule, dict) else None
        hi = rule.get("max") if isinstance(rule, dict) else None
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise DeskError(
                ErrorCode.VALUE_OUT_OF_RANGE,
                f"'{name}' muss zwischen {lo} und {hi} liegen",
                {"param": name, "value": value, "min": lo, "max": hi},
            )
        return
    if kind == "str":
        if not isinstance(value, str):
            raise DeskError(ErrorCode.INVALID_PARAMS, f"'{name}' muss Text sein")
        return
    raise DeskError(ErrorCode.INVALID_PARAMS, f"unbekannter Parametertyp für '{name}': {kind!r}")
