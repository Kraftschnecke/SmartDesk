# Smart Desk

Selbstgebauter Smart Desk, gesteuert von J.A.R.V.I.S.
Architektur: Jarvis → Desk API → **Desk Controller** (Raspberry Pi) → **Manager** (ESP32/Mega) → Hardware.

## Ordner

```
controller/   Desk Controller (Python, läuft auf dem Pi)
  protocol.py        Nachrichten bauen/prüfen (docs/DESIGN_PROTOCOL.md)
  transport.py       USB-Seriell + Speicher-Verbindung für Tests
  manager_link.py    Verbindung zu einem Manager: Anfragen, Timeout, Heartbeat, Handshake
  desk_state.py      DESK_STATE: der tatsächliche Zustand des Tisches
  controller.py      Prüfen, Routen, feste Regeln (Taster, Not-Aus, adjust ohne Wrap-Around)
  simulated_light.py simulierter LightManager (Referenz für die Firmware)
  main.py            Start + Eingabezeile zum Testen
firmware/     Manager-Firmware (PlatformIO)
test/         automatische Tests (ohne Hardware)
docs/         Design-Dokumente
```

## Einrichten (Pi)

```
cd ~/SmartDesk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Starten

Ohne Hardware (simulierter LightManager):

```
python -m controller.main --simulate
```

Mit echtem Manager: `managers.example.json` nach `managers.json` kopieren und den Anschluss
eintragen (`ls /dev/serial/by-id/`), dann:

```
python -m controller.main
```

## Testen

```
python run_tests.py        # muss mit ALL TESTS PASSED enden
python run_tests.py -v
```

Regel: Tests brauchen nie Hardware, Netzwerk oder pyserial.
