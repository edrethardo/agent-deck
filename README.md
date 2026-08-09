# agent-deck 🦑

**A physical traffic-light for your Claude Code sessions, on a [DeepDeck](https://deepdeck.co/) macropad.**

Run half a dozen Claude Code sessions in parallel and you lose track of which one is working, which one finished, and which one has been silently waiting ten minutes for you to approve a command. agent-deck puts every session on its own key of a WiFi macropad sitting on your desk:

- 🟢 **green** — session is idle and available
- 🟡 **yellow** — Claude is working
- 🔴 **red** — Claude is **blocked waiting for your input** (permission prompt, question, plan approval)
- 🔵 **blue** — session detected, but it started before the hooks were installed (reload it to get live status)

The OLED shows a kraken until you need it. Press a key to see which project lives there; double-press to **jump straight to that session's window** on your desktop; hold a key to rearrange the board; the left knob dims the LEDs.

No cloud, no MQTT broker, no Home Assistant — your PC talks to the pad directly over the LAN via ESPHome's encrypted native API.

## How it works

```
Claude Code sessions ──hooks──▶  agent-monitor daemon  ──ESPHome API / WiFi──▶  DeepDeck
 (terminal, VS Code)                │      │       ▲                            16 RGB keys
        │                           │      │       └── key events               + OLED + knob
        └── /proc scan (pre-hook    │      └── state.json ◀── agent-monitor
            sessions, blue)         │                          status --watch
                                    └── systemd user service
```

Claude Code [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) fire on session start/prompt/notification/stop and send one JSON line to a Unix socket. The daemon keeps a registry (16 stable key slots, dead-process pruning, atomic state file), renders LED colors + OLED text, and pushes the full display state to the pad on every change. A periodic `/proc` scan also discovers sessions that predate the hook installation. The pad itself is a dumb display — all logic lives on the PC, so firmware updates are rare and go over the air.

## Hardware

One [DeepDeck Ahuyama](https://github.com/DeepSea-Developments/DeepDeck.Ahuyama.hw) (open-source ESP32 macropad: 16 hot-swap keys with SK6812 RGB, SSD1306 OLED, 2 rotary encoders). The stock firmware is replaced with an ESPHome build — flashing back is always possible, nothing is modified irreversibly.

## Installation

Requirements: Linux with systemd (user session), X11 for window focusing, [uv](https://docs.astral.sh/uv/), Python ≥ 3.14 (uv fetches it automatically), `wmctrl` (`sudo apt install wmctrl`) for double-press-to-focus, and a 2.4 GHz WiFi network shared with the pad.

### 1. PC side

```bash
git clone https://github.com/edrethardo/agent-deck.git && cd agent-deck
uv tool install --editable .          # installs ~/.local/bin/agent-monitor
python3 scripts/install-hooks.py      # registers Claude Code hooks (backs up settings.json)
mkdir -p ~/.config/systemd/user
cp systemd/agent-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-monitor
```

Sessions started before this point show as blue until you reload their window (hooks load at session start).

### 2. Firmware (first flash over USB, afterwards OTA)

```bash
cp firmware/secrets.yaml.example firmware/secrets.yaml
# edit firmware/secrets.yaml: WiFi credentials + fresh keys (openssl rand -base64 32)
uvx esphome run firmware/deepdeck.yaml        # pick the /dev/ttyUSB0 port
```

Later updates need no cable: `uvx esphome run firmware/deepdeck.yaml --device deepdeck.local`.

> ⚠️ If the board looks completely dead after plugging in, unplug, make sure no key is held down, and re-plug. A held key on GPIO12 (an ESP32 strapping pin) can prevent booting entirely — a property of the PCB, not of this firmware.

### 3. Connect daemon and pad

`~/.config/agent-monitor/config.toml`:

```toml
[pad]
enabled = true
host = "deepdeck.local"      # or a fixed IP
api_key = "<same key as in firmware/secrets.yaml>"
```

```bash
systemctl --user restart agent-monitor
systemctl --user stop agent-monitor && agent-monitor test-pattern   # LED chase + colors
systemctl --user start agent-monitor
```

Without the config file the daemon runs CLI-only — useful before the hardware arrives.

**Version coupling:** daemon and firmware move together. If an update changes the `set_state` arguments, reflash OTA and restart the daemon back-to-back — until then the daemon logs `pad firmware mismatch` and keeps retrying harmlessly.

## Using the deck

| Gesture | Effect |
|---|---|
| **Press** a key | OLED shows `key N` + that session's project name for 3 s |
| **Double-press** (<400 ms) | Focuses that session's window on your desktop (VS Code windows match best) |
| **Hold** (≥600 ms) | Pick up the session — the key blinks; **click another key** to move/swap it there; same key or 10 s cancels |
| **Left knob** | LED brightness in 5 % steps (persists across reboots) |

The OLED idles on the DeepDeck kraken; sessions keep their key for their whole lifetime, across daemon restarts and session reloads.

## CLI

```bash
agent-monitor status           # colored table: key, project, status, duration
agent-monitor status --watch   # live view
agent-monitor test-pattern     # hardware smoke test (requires daemon stopped)
```

## Troubleshooting

- **A key stays blue although the session is active** — that session started before the hooks existed. Reload its VS Code window (`Ctrl+Shift+P` → *Developer: Reload Window*); the conversation is kept, tracking starts immediately.
- **`pad firmware mismatch` in `journalctl --user -u agent-monitor`** — reflash OTA and restart the daemon (see version coupling above).
- **Double-press does nothing** — is `wmctrl` installed? Does `systemctl --user show-environment` contain `DISPLAY`? Check the journal for `focus request` lines.
- **More than 16 sessions** — extras get no key (overflow) but stay visible in the CLI; real sessions evict blue scanned entries when keys run short.
- **Left knob does nothing** — encoders 1/2 may be swapped on your build; change pins 25/26 to 33/32 in `firmware/deepdeck.yaml` and reflash OTA.
- **Keys never turn red in VS Code** — known upstream limitation (verified empirically 2026-08): the VS Code extension's inline permission dialog does not fire Claude Code's `Notification` hook, focused or not, so the daemon never learns the session is blocked. Terminal (`claude` CLI) sessions turn red as designed. If a future extension version adds the hook, red starts working with no changes here — `journalctl --user -u agent-monitor | grep notification` will show the events arriving.

## Development

```bash
uv run pytest -q                                # 98 tests
uvx esphome config firmware/deepdeck.yaml       # firmware schema check
uvx esphome compile firmware/deepdeck.yaml      # full build
```

Design documents live in `docs/superpowers/` — the spec records every decision and hardware finding, including why the key matrix needs a custom scan (the PCB has no pull resistors; the firmware replicates the stock firmware's weak-drive-contention method).

## License

[MIT](LICENSE)

## Acknowledgments

- [DeepSea Developments](https://www.deepseadev.com/) for the open-source DeepDeck — pin mappings and the key-scan method were derived from their [stock firmware](https://github.com/DeepSea-Developments/DeepDeck.Ahuyama.fw), and the kraken artwork comes from their bottom-plate design.
- Built with [ESPHome](https://esphome.io/), [aioesphomeapi](https://github.com/esphome/aioesphomeapi), and [Claude Code](https://claude.com/claude-code).
