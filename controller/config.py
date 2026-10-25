"""Konfiguration: welche Manager an welchem Anschluss hängen.

Die Datei managers.json ist pro Gerät verschieden und steht nicht in Git.
Vorlage: managers.example.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "managers.json"


@dataclass
class ManagerConfig:
    name: str
    port: str
    baudrate: int = 115200


def load_managers(path: Path = DEFAULT_CONFIG) -> list[ManagerConfig]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} fehlt. Kopiere managers.example.json nach managers.json und trage den Anschluss ein "
            f"(ls /dev/serial/by-id/)."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{path}: erwartet ein Objekt wie {{\"light\": {{\"port\": \"/dev/...\"}}}}")
    managers = []
    for name, entry in raw.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("port"), str):
            raise ValueError(f"{path}: Manager '{name}' braucht einen 'port'")
        managers.append(ManagerConfig(name, entry["port"], int(entry.get("baudrate", 115200))))
    return managers
