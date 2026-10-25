"""Desk Controller starten, mit einer kleinen Eingabezeile zum Testen.

    python -m controller.main --simulate     ohne Hardware (simulierter LightManager)
    python -m controller.main                mit echten Managern aus managers.json

Befehle in der Eingabezeile: siehe HELP unten.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from .controller import DeskController
from .manager_link import ManagerLink
from .protocol import DeskError

HELP = """\
Befehle:
  state                         gesamten DESK_STATE anzeigen
  <manager>.<action> [wert]     Befehl ausführen, z. B.
                                  light.set_brightness 60
                                  light.set_power on
                                  light.adjust_brightness -10
                                  light.reset
  query <manager> [ziel]        direkt beim Manager nachfragen (state, status, identity, capabilities)
  events                        letzte Events anzeigen
  help                          diese Hilfe
  quit                          beenden
Nur mit --simulate:
  button                        Taster am LightManager drücken
  estop on | estop off          Not-Aus drücken / lösen
  reboot                        Neustart des Managers simulieren
"""


def parse_value(raw: str) -> Any:
    low = raw.lower()
    if low in ("on", "an", "true", "ja"):
        return True
    if low in ("off", "aus", "false", "nein"):
        return False
    try:
        return int(raw)
    except ValueError:
        return raw


def build_params(controller: DeskController, manager: str, action: str, args: list[str]) -> dict:
    """Positionsargumente den Parameternamen aus den Capabilities zuordnen."""
    if action.startswith("adjust_"):
        names = ["delta"]
    else:
        try:
            info = controller.desk_state.manager(manager)
        except KeyError:
            return {}
        names = list(info.capabilities.get("actions", {}).get(action, {}).get("params", {}))
    params = {}
    for arg in args:
        if "=" in arg:
            key, _, val = arg.partition("=")
            params[key] = parse_value(val)
    positional = [a for a in args if "=" not in a]
    for name, val in zip(names, positional):
        params.setdefault(name, parse_value(val))
    return params


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def wait_for_managers(controller: DeskController, timeout: float) -> None:
    """Beim Start kurz warten, bis alle Manager den Handshake abgeschlossen haben."""
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end and not all(link.is_ready for link in controller.links.values()):
        time.sleep(0.05)
    for name, link in controller.links.items():
        status = controller.desk_state.manager(name).status
        print(f"  {name}: {status}" + ("" if link.is_ready else "  (noch nicht verbunden – versucht es weiter)"))


def run_repl(controller: DeskController, sim: Optional[Any]) -> None:
    print("Smart Desk Controller läuft. 'help' für Befehle.")
    while True:
        try:
            line = input("desk> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0], parts[1:]
        try:
            if cmd in ("quit", "exit"):
                return
            elif cmd == "help":
                print(HELP)
            elif cmd == "state":
                print_json(controller.desk_state.snapshot())
            elif cmd == "events":
                print_json(controller.events[-20:])
            elif cmd == "query" and args:
                print_json(controller.query(args[0], args[1] if len(args) > 1 else "state"))
            elif cmd in ("button", "estop", "reboot"):
                if sim is None:
                    print("Nur mit --simulate verfügbar.")
                elif cmd == "button":
                    sim.press_button()
                elif cmd == "reboot":
                    sim.boot()
                else:
                    sim.set_estop(bool(args) and args[0].lower() in ("on", "an", "1"))
            elif "." in cmd:
                manager, _, action = cmd.partition(".")
                params = build_params(controller, manager, action, args)
                print_json(controller.execute(manager, action, params))
            else:
                print("Unbekannter Befehl. 'help' für Hilfe.")
        except DeskError as exc:
            print(f"FEHLER {exc.code}: {exc.message}")
            if exc.details:
                print_json(exc.details)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Smart Desk Controller")
    parser.add_argument("--simulate", action="store_true", help="ohne Hardware, mit simuliertem LightManager")
    parser.add_argument("--config", type=Path, default=None, help="Pfad zu managers.json")
    parser.add_argument("--debug", action="store_true", help="ausführliche Ausgabe")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    controller = DeskController()
    sim = None

    if args.simulate:
        from .simulated_light import SimulatedLightManager
        from .transport import MemoryTransport

        ctrl_end, sim_end = MemoryTransport.pair()
        controller.add_link(ManagerLink("light", ctrl_end, controller.desk_state))
        sim = SimulatedLightManager(sim_end)
    else:
        from .config import DEFAULT_CONFIG, load_managers
        from .transport import SerialTransport

        try:
            import serial  # noqa: F401
        except ImportError:
            print("pyserial fehlt: pip install -r requirements.txt", file=sys.stderr)
            return 1

        try:
            configs = load_managers(args.config or DEFAULT_CONFIG)
        except (FileNotFoundError, ValueError) as exc:
            print(exc, file=sys.stderr)
            return 1
        for cfg in configs:
            controller.add_link(ManagerLink(cfg.name, SerialTransport(cfg.port, cfg.baudrate), controller.desk_state))

    controller.start()
    if sim is not None:
        sim.start()
    wait_for_managers(controller, timeout=5.0)
    try:
        run_repl(controller, sim)
    finally:
        if sim is not None:
            sim.stop()
        controller.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
