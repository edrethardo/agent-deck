# Agent Monitor — Design

**Datum:** 2026-08-08
**Status:** Vom User freigegebenes Design

## Problem

Aaron fährt parallel mehrere Claude-Code-Sessions (Terminal und VSCode, verschiedene Projekte) und sieht nicht, welche Session gerade arbeitet, fertig ist oder auf Input wartet. Gewünscht ist eine Ampel-Anzeige pro Session:

- 🟢 **Grün** — verfügbar: Session ist offen und wartet auf einen (neuen) Prompt; auch direkt nach Abschluss einer Aufgabe.
- 🟡 **Gelb** — busy: Claude arbeitet gerade.
- 🔴 **Rot** — erwarte Input: Claude hängt *mitten in der Arbeit* und braucht Aaron (Permission-Prompt, Rückfrage, Plan-Freigabe). Explizit **nicht** rot: fertig und wartet auf den nächsten Prompt — das ist grün.

## Entscheidungen (aus dem Brainstorming)

1. **Hardware-Anzeige:** DeepDeck (Open-Source-ESP32-Macropad von DeepSea Developments). Specs: ESP32-WROOM-32D, WiFi, 18× SK6812-RGB-LEDs (16 Tasten), OLED 0,96" SSD1306 128×64 (I2C), 2× EC11-Encoder, CP2102N USB-Serial. Quelle: https://deepdeck.co/en/QuickStartGuide/hw-specs/
2. **Eine Taste/LED pro Session** (kein Aggregat) — bis zu 16 Sessions.
3. **Rot nur bei Blockierung** (siehe oben).
4. **Anzeige:** DeepDeck **plus** Terminal-Ansicht (CLI).
5. **Firmware darf ersetzt werden** — die Macropad-Funktion wird nicht benötigt.
6. **Gewählter Ansatz:** ESPHome-Firmware auf dem Pad + ESPHome **native API** vom PC aus (kein MQTT-Broker). Verworfen: ESPHome+MQTT (Broker als zusätzliche Infrastruktur ohne aktuellen Nutzen), Fork der Stock-Firmware (deutlich mehr Aufwand, unnötig da Firmware ersetzbar).

## Architektur

```
Claude Sessions ──Hooks──▶ agent-monitor daemon ──ESPHome-API (WiFi)──▶ DeepDeck
 (Terminal, VSCode)            │        │                                16 LEDs + OLED
                               │        └── state.json ◀── agent-monitor status (CLI)
                               └── systemd user service
```

Claude-Code-Hooks melden Statuswechsel an einen lokalen Daemon. Der Daemon hält den Zustand aller Sessions, schiebt ihn ans DeepDeck und schreibt ihn in eine State-Datei, aus der die CLI liest. Das Pad ist eine dumme Anzeige ohne eigene Logik.

## Komponenten

Ein Python-Paket `agent-monitor` (Python 3.12, `uv`) mit drei Einstiegspunkten, plus eine ESPHome-Konfiguration.

### 1. ESPHome-Firmware (`firmware/deepdeck.yaml`)

- Basis: `esp32` (WROOM-32D), WiFi, `api` (mit Verschlüsselung), `ota`.
- 18 SK6812-LEDs als adressierbarer Strip (`esp32_rmt_led_strip`), SSD1306-OLED über I2C.
- Ein benutzerdefinierter API-Service nimmt den kompletten Anzeigezustand entgegen: LED-Farben (Array) + OLED-Textzeilen. Keine Logik auf dem ESP32.
- Pin-Belegung (LED-Datenpin, I2C-Pins) wird aus der offenen Stock-Firmware übernommen: https://github.com/DeepSea-Developments/DeepDeck.Ahuyama.fw *(bei Implementierung zu extrahieren)*.

### 2. Daemon (`agent-monitor daemon`)

- Läuft als systemd user service.
- Lauscht auf einem Unix-Socket (unter `$XDG_RUNTIME_DIR/agent-monitor/`) auf Hook-Events.
- Zustand pro Session: `session_id`, Status, Projektpfad (`cwd`), PID des Claude-Prozesses, Slot (Taste 1–16), Zeitstempel des letzten Events.
- Slot-Vergabe: neue Session → niedrigster freier Slot; bleibt für die Lebensdauer der Session stabil.
- Bei jeder Zustandsänderung: LEDs + OLED über `aioesphomeapi` aktualisieren und `state.json` atomar schreiben.

### 3. Hook-Client (`agent-monitor hook`)

- Wird von Claude Code bei `SessionStart`, `UserPromptSubmit`, `Notification`, `Stop`, `SessionEnd` aufgerufen; global registriert in `~/.claude/settings.json` (gilt für alle Projekte).
- Liest das Hook-JSON von stdin, ergänzt die PID des Claude-Prozesses (Parent-PID des Hook-Prozesses), schickt alles an den Socket.
- Fire-and-forget mit kurzem Timeout; alle Fehler werden geschluckt. **Der Hook darf Claude nie blockieren oder Fehler produzieren** — auch wenn der Daemon nicht läuft.

### 4. CLI (`agent-monitor status [--watch]`)

- Liest `state.json`, zeigt eine farbige Tabelle: Slot, Projekt (Verzeichnisname), Status, Dauer im aktuellen Status.
- `--watch`: Live-Aktualisierung.
- Meldet klar, wenn der Daemon nicht läuft.

Zusätzlich: `agent-monitor test-pattern` fährt alle LEDs einmal durch grün/gelb/rot (Hardware-Smoke-Test).

## Statuslogik

| Hook-Event | Neuer Status |
|---|---|
| `SessionStart` | 🟢 grün |
| `UserPromptSubmit` | 🟡 gelb |
| `Notification` (Permission/Rückfrage) | 🔴 rot |
| `Stop` | 🟢 grün |
| `SessionEnd` | Session entfernt, Slot frei, LED aus |

- `Notification`-Events werden am Nachrichtentext gefiltert: Permission-Anfragen/Rückfragen → rot; reine Idle-Meldungen („waiting for your input" nach Untätigkeit am Prompt) ändern den Status **nicht**, damit unbenutzte Sessions grün bleiben. *Die exakten Payload-Texte werden bei der Implementierung mit echten Sessions verifiziert.*
- Unbekannte/irrelevante Events werden ignoriert.

## OLED-Anzeige

Eine Textzeile pro aktiver Session: `<Slot> <Projektname> <Status-Symbol>`. Maximal 8 Zeilen (Displayhöhe); weitere Sessions werden abgeschnitten — die LEDs zeigen weiterhin alle 16 Slots.

## Fehlerbehandlung

- **Daemon läuft nicht:** Hooks verwerfen ihr Event kommentarlos; CLI meldet „Daemon läuft nicht".
- **Pad offline / WiFi weg:** Daemon reconnected dauerhaft (Reconnect-Logik von `aioesphomeapi`) und pusht nach jedem Reconnect den *kompletten* Zustand, nicht Deltas. Kein veralteter LED-Stand.
- **Session hart beendet** (`SessionEnd` fehlt): Daemon prüft periodisch (~ alle 15 s) die Claude-PIDs über `/proc`; tote Sessions werden entfernt.
- **Daemon-Neustart:** Zustand wird aus `state.json` geladen und sofort per PID-Check bereinigt.
- **Mehr als 16 Sessions:** Overflow-Sessions ohne LED, aber sichtbar in der CLI.

## Testing

- **Unit-Tests (TDD):** State-Machine (Event → Statusübergang), Slot-Vergabe/-Freigabe, Staleness-Bereinigung, Notification-Filterung, Parsing der Hook-Payloads.
- **Daemon-Tests:** gegen ein Fake-Pad (gemocktes `aioesphomeapi`), inkl. Reconnect-pusht-vollen-Zustand.
- **Hardware-Smoke-Test:** ESPHome-Config kompiliert; `agent-monitor test-pattern` auf dem echten Pad.

## Bewusst nicht in v1

- Tasten des Pads lösen nichts aus (z. B. „Taste drücken → Terminal der Session fokussieren" wäre ein späteres Feature).
- Keine Encoder-Funktionen, kein Batteriebetrieb-Feintuning.
- Keine GUI über die CLI hinaus, kein MQTT, kein Home Assistant.

## Offene Punkte für die Implementierung

1. Pin-Belegung (LED-Datenpin, I2C) aus der Stock-Firmware extrahieren.
2. Exakte `Notification`-Payload-Texte mit echten Sessions verifizieren.
3. WiFi-Zugangsdaten und API-Key: lokal in `firmware/secrets.yaml` (nicht committen; `.gitignore`).
