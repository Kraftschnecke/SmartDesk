# Smart Desk – Protokoll Controller ↔ Manager

**Status:** Entwurf v1 – zur Abstimmung, noch kein Code
**Gilt für:** Kommunikation zwischen Desk Controller (Raspberry Pi) und Managern (ESP32 / Mega)
**Gilt nicht für:** Desk API zwischen Jarvis und Controller (eigenes Dokument, später)

---

## 1. Grundidee

```text
Jarvis ──Desk API──► Controller ──dieses Protokoll──► Manager ──► Hardware
```

- Der Controller spricht mit jedem Manager über eine **eigene Punkt-zu-Punkt-Verbindung**.
- Der Manager kennt die Hardware, der Controller nur die **Fähigkeiten** des Managers.
- Jede Antwort enthält den **tatsächlichen** Zustand, nie den angeforderten.

---

## 2. Transport

| Punkt | Festlegung |
|---|---|
| Verbindung | USB-Seriell (ESP32 ↔ Pi) |
| Geschwindigkeit | 115200 Baud, 8N1 |
| Format | **Eine JSON-Nachricht pro Zeile** (NDJSON), UTF-8, Zeilenende `\n` |
| Maximale Länge | 512 Byte pro Zeile |
| Bibliotheken | Pi: `pyserial` + `json` · ESP32: `ArduinoJson` |

Warum JSON: lesbar beim Debuggen (`screen /dev/ttyUSB0 115200`), in Python und auf dem ESP32 einfach.
Ein binäres Format lohnt sich erst, wenn die Geschwindigkeit nachweislich nicht reicht.

Später kann der Transport wechseln (z. B. RS-485 oder Ethernet), das **Nachrichtenformat bleibt gleich**.

---

## 3. Aufbau jeder Nachricht

```json
{"v": 1, "type": "COMMAND", "id": 7, ...}
```

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `v` | ja | Protokollversion, aktuell `1` |
| `type` | ja | Nachrichtentyp, siehe Abschnitt 4 |
| `id` | bei Anfragen und Antworten | Zahl, vom Controller vergeben; die Antwort trägt dieselbe `id` |
| `seq` | bei EVENT | laufende Nummer des Managers; Lücken = verlorene Events |

Unbekannte Felder werden **ignoriert** (damit spätere Versionen kompatibel bleiben).

---

## 4. Nachrichtentypen

### Controller → Manager

| Typ | Zweck | Beispiel |
|---|---|---|
| `COMMAND` | Aktion ausführen | `{"v":1,"type":"COMMAND","id":7,"action":"set_brightness","params":{"value":60}}` |
| `QUERY` | Wert/Zustand abfragen | `{"v":1,"type":"QUERY","id":8,"target":"state"}` |
| `PING` | Lebenszeichen | `{"v":1,"type":"PING","id":9}` |

Erlaubte `target` für QUERY bei **jedem** Manager: `identity`, `capabilities`, `state`, `status`.

### Manager → Controller

| Typ | Zweck | Beispiel |
|---|---|---|
| `RESPONSE` | Erfolgreiche Antwort auf COMMAND/QUERY | `{"v":1,"type":"RESPONSE","id":7,"data":{"power":true,"brightness":60}}` |
| `ERROR` | Anfrage nicht ausführbar oder Fehler | `{"v":1,"type":"ERROR","id":7,"code":"VALUE_OUT_OF_RANGE","message":"brightness 0-100","details":{"value":130,"min":0,"max":100}}` |
| `EVENT` | Etwas ist passiert | `{"v":1,"type":"EVENT","seq":12,"event":"BUTTON_PRESSED","data":{"button":"light"}}` |
| `PONG` | Antwort auf PING | `{"v":1,"type":"PONG","id":9,"status":"READY"}` |

---

## 5. Regeln

1. **Jede Anfrage bekommt genau eine Antwort**: `RESPONSE`, `ERROR` oder `PONG` mit derselben `id`.
2. **Timeout:** Kommt innerhalb von **500 ms** keine Antwort, gilt die Anfrage beim Controller als `TIMEOUT`.
   Der Controller wiederholt einen COMMAND **nicht blind**, sondern fragt zuerst den Zustand ab (QUERY `state`).
3. **Ist-Zustand zurückmelden:** Die RESPONSE auf einen COMMAND enthält den Zustand **nach** der Ausführung.
4. **Doppelte Prüfung:** Der Controller prüft Grenzen anhand der Capabilities. Der Manager prüft trotzdem selbst noch einmal.
   Ungültige Werte werden **nicht** stillschweigend umgebrochen (kein Wrap-Around).
5. **Keine eigenen Entscheidungen:** Ein Manager führt nur Befehle des Controllers aus.
   Ausnahme: **lokale Sicherheitsreaktionen** (Not-Aus, Überlast, Blockade). Danach meldet er ein EVENT.
6. **Lokale Eingaben gehen über den Controller:** Drückt jemand einen Taster am Manager, meldet der Manager
   `BUTTON_PRESSED`. Der Controller entscheidet und schickt den passenden COMMAND zurück.
7. **Kaputte Nachrichten:** Ungültiges JSON oder zu lange Zeilen → `ERROR` mit `code: PARSE_ERROR` und `id: null`.
   Der Manager stürzt dabei nie ab und verwirft nur diese Zeile.

---

## 6. Verbindungsaufbau und Lebenszeichen

### Start

```text
Manager startet
  → EVENT BOOT  {"manager":"light","firmware":"0.1.0","protocol":1}
Controller
  → QUERY identity
  → QUERY capabilities
  → QUERY state
  → Manager im DESK_STATE als READY eintragen
```

Der Controller schickt beim Start eines Managers **keine alten Befehle** hinterher. Er übernimmt den tatsächlichen Zustand.

### Heartbeat

- Der Controller sendet **jede Sekunde** einen `PING`.
- **Controller-Seite:** Drei verpasste PONGs (≈ 3 s) → Manager im DESK_STATE als `OFFLINE`.
- **Manager-Seite:** 3 s keine Nachricht vom Controller → Status `CONTROLLER_LOST`.
  Der Manager hält den aktuellen sicheren Zustand, führt laufende Bewegungen sicher zu Ende bzw. stoppt sie
  und nimmt erst nach neuem Kontakt wieder Befehle an.

---

## 7. Manager-Status

| Status | Bedeutung | Status-LED (später) |
|---|---|---|
| `STARTING` | Manager initialisiert sich | blinkt |
| `READY` | alles in Ordnung | grün |
| `WARNING` | funktioniert, braucht Aufmerksamkeit | gelb |
| `ERROR` | Funktion gestört, betroffener Teil abgeschaltet | rot |
| `EMERGENCY` | Not-Aus ausgelöst | rot blinkend |
| `CONTROLLER_LOST` | keine Verbindung zum Controller | gelb blinkend |

---

## 8. Not-Aus-Ablauf

```text
Not-Aus gedrückt
  → Hardware trennt den Strom der LED (unabhängig von Software)
  → Manager erkennt es am Meldepin
  → EVENT ESTOP_ACTIVE, Status EMERGENCY
  → jeder COMMAND wird mit ERROR ESTOP_ACTIVE abgelehnt

Not-Aus gelöst
  → EVENT ESTOP_RELEASED, Status bleibt EMERGENCY (kein automatischer Neustart)

Bewusster Reset
  → Controller: COMMAND reset
  → Manager prüft seine Hardware
  → RESPONSE mit Status READY  oder  ERROR mit Grund
```

Im Prototyp ist der Not-Aus ein einfacher Taster. Der echte Tisch bekommt einen einrastenden Not-Aus-Schalter (Öffner).

---

## 9. Fehlercodes

| Code | Bedeutung |
|---|---|
| `PARSE_ERROR` | Nachricht ist kein gültiges JSON oder zu lang |
| `UNSUPPORTED_VERSION` | `v` wird nicht unterstützt |
| `UNKNOWN_TYPE` | unbekannter `type` |
| `UNKNOWN_ACTION` | unbekannte `action` oder unbekanntes `target` |
| `INVALID_PARAMS` | Parameter fehlen oder haben den falschen Typ |
| `VALUE_OUT_OF_RANGE` | Wert außerhalb der erlaubten Grenzen (mit `details`) |
| `NOT_READY` | Manager ist nicht im Status READY |
| `ESTOP_ACTIVE` | Not-Aus aktiv, Befehl abgelehnt |
| `HARDWARE_FAULT` | Hardware antwortet nicht oder meldet einen Fehler |
| `TIMEOUT` | nur Controller-seitig: keine Antwort in 500 ms |

---

## 10. Erster Manager: LightManager (v1)

### Identity

```json
{"manager": "light", "firmware": "0.1.0", "protocol": 1, "uid": "esp32-<chip-id>"}
```

### Capabilities

```json
{
  "actions": {
    "set_power":      {"params": {"on": "bool"}},
    "set_brightness": {"params": {"value": {"type": "int", "min": 0, "max": 100}}},
    "reset":          {"params": {}}
  },
  "events": ["BOOT", "STATE_CHANGED", "BUTTON_PRESSED", "ESTOP_ACTIVE", "ESTOP_RELEASED"]
}
```

### State

```json
{"power": true, "brightness": 60}
```

- `set_power(false)` schaltet aus, die Helligkeit bleibt gespeichert.
- `set_brightness` bei ausgeschaltetem Licht speichert nur den Wert, schaltet aber **nicht** ein.

### Beispielablauf

```text
→ {"v":1,"type":"COMMAND","id":21,"action":"set_brightness","params":{"value":60}}
← {"v":1,"type":"RESPONSE","id":21,"data":{"power":true,"brightness":60}}

(Taster am ESP32 gedrückt)
← {"v":1,"type":"EVENT","seq":5,"event":"BUTTON_PRESSED","data":{"button":"light"}}
→ {"v":1,"type":"COMMAND","id":22,"action":"set_power","params":{"on":false}}
← {"v":1,"type":"RESPONSE","id":22,"data":{"power":false,"brightness":60}}

→ {"v":1,"type":"COMMAND","id":23,"action":"set_brightness","params":{"value":130}}
← {"v":1,"type":"ERROR","id":23,"code":"VALUE_OUT_OF_RANGE","message":"brightness 0-100","details":{"value":130,"min":0,"max":100}}
```

---

## 11. Zur Abstimmung offen

- [ ] JSON pro Zeile über USB-Seriell mit 115200 Baud – einverstanden?
- [ ] Heartbeat 1 s, Verbindungsverlust nach 3 s – passend?
- [ ] Taster am Manager entscheiden **nicht** selbst, sondern laufen über den Controller – einverstanden?
- [ ] Not-Aus-Reset nur per bewusstem `reset`-Befehl – einverstanden?

Danach: Controller auf dem Pi (Schritt 2), dann LightManager-Firmware (Schritt 3).
