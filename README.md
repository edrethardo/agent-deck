# agent-monitor

Traffic-light status for Claude Code sessions: 🟢 available · 🟡 working · 🔴 waiting for input.
Displayed on a DeepDeck macropad (ESPHome, 16 key LEDs + OLED) and as a CLI.

## Installation (PC)

    uv tool install --editable .          # installs ~/.local/bin/agent-monitor
    python3 scripts/install-hooks.py      # registers Claude Code hooks
    mkdir -p ~/.config/systemd/user
    cp systemd/agent-monitor.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now agent-monitor

Running Claude sessions only show up after being restarted
(hooks are loaded at session start).

## Pad configuration (~/.config/agent-monitor/config.toml)

    [pad]
    enabled = true
    host = "deepdeck.local"      # or a fixed IP
    api_key = "<same key as in firmware/secrets.yaml>"

Without this file the daemon runs without hardware (CLI only).

## Flashing the firmware (once, over USB)

    cp firmware/secrets.yaml.example firmware/secrets.yaml   # fill it in!
    uvx esphome run firmware/deepdeck.yaml                   # afterwards: OTA over WiFi

The daemon and firmware move together: if a daemon update changes the set_state arguments, reflash the pad (after the first USB flash this works over WiFi: uvx esphome run firmware/deepdeck.yaml) — until then the daemon logs "pad firmware mismatch" and keeps retrying.

## Usage

    agent-monitor status           # table
    agent-monitor status --watch   # live
    agent-monitor test-pattern     # LED test on the pad
