# agent-deck 🦑

**A physical traffic-light for your Claude Code sessions, on a [DeepDeck](https://deepdeck.co/) macropad.**

Run half a dozen Claude Code sessions in parallel and you lose track of which one is working, which one finished, and which one has been silently waiting ten minutes for you to approve a command. agent-deck puts every session on its own key of a WiFi macropad sitting on your desk:

- 🟢 **green** — **recently finished a task**: a fresh result is waiting for you (fades to blue/white after 10 min)
- 🟡 **yellow** — Claude is working
- 🔴 **red** — Claude is **blocked waiting for your input** (permission prompt, question, plan approval)
- 🔵 **blue** — session is idle and **remote-controllable**: pick it up from your phone
- ⚪ **white** — session is idle but only reachable at the PC (includes sessions from before the hooks were installed — reload those to get live status)

Red works **in VS Code too** — permission prompts arrive via Claude Code's permission hooks, and questions/plan approvals are detected straight from the session transcript. Sessions running **autonomously** (background subagents, multi-agent runs) stay yellow even though no hooks fire for that work — so a green key on a long run means the run has genuinely stopped and is waiting for you, not that you missed a notification.

The OLED idles on your **account usage** — the same 5-hour and weekly percentages `/usage` shows, as bars you can read across the desk. Press a key to see which project lives there plus its **model, reasoning effort and context usage** (handy on the phone, which shows none of those); double-press to **jump straight to that session's window** on your desktop; hold a key to rearrange the board; the knobs handle brightness, the context menu, and the notification LEDs.

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

Claude Code [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) fire on session start/prompt/notification/stop and on the permission-dialog lifecycle, and send one JSON line to a Unix socket. The daemon keeps a registry (16 stable key slots, dead-process pruning, atomic state file), renders LED colors + OLED text, and pushes the full display state to the pad on every change. A periodic `/proc` scan discovers sessions that predate the hook installation, and a 10 s tick reads each session's transcript tail for context %, model, pending questions, and signs of autonomous activity. The pad itself is a dumb display — all logic lives on the PC, so firmware updates are rare and go over the air.

Everything is **read-only** against Claude Code's own files (`~/.claude/`): transcripts, session metadata, and the usage cache. The only network call the daemon ever makes is an optional usage fetch to `api.anthropic.com` (same endpoint the phone app uses, with the OAuth token already on disk, at most once per 10 minutes) when the local cache is stale. Nothing is written into Claude Code's state, and nothing leaves your machine otherwise.

## Hardware

One [DeepDeck Ahuyama](https://github.com/DeepSea-Developments/DeepDeck.Ahuyama.hw) (open-source ESP32 macropad: 16 hot-swap keys with SK6812 RGB, SSD1306 OLED, 2 rotary encoders). The stock firmware is replaced with an ESPHome build — flashing back is always possible, nothing is modified irreversibly.

## Installation

Requirements: Linux with systemd (user session), X11 for the desktop actions, [uv](https://docs.astral.sh/uv/), Python ≥ 3.14 (uv fetches it automatically), `wmctrl` + `xdotool` (`sudo apt install wmctrl xdotool`) for window focusing and the pad menu actions, and a 2.4 GHz WiFi network shared with the pad.

### 1. PC side

```bash
git clone https://github.com/edrethardo/agent-deck.git && cd agent-deck
uv tool install --editable .          # installs ~/.local/bin/agent-monitor
python3 scripts/install-hooks.py      # registers Claude Code hooks (backs up settings.json);
                                      # re-run after updates — new hook events register the same way
mkdir -p ~/.config/systemd/user
cp systemd/agent-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-monitor
```

Sessions started before this point show as blue until you reload their window (hooks load at session start).

### 2. Firmware (first flash over USB, afterwards OTA)

The WiFi credentials are baked into the firmware at flash time — the pad joins your network by itself on every boot, no pairing or app involved:

```bash
cp firmware/secrets.yaml.example firmware/secrets.yaml
# edit firmware/secrets.yaml:
#   wifi_ssid / wifi_password — must be a 2.4 GHz network (ESP32 has no 5 GHz);
#                               the PC only needs to reach the pad over the LAN,
#                               it does not need WiFi itself
#   api_key  — openssl rand -base64 32 (reused in config.toml below)
#   ota_password — any secret; protects over-the-air updates
uvx esphome run firmware/deepdeck.yaml        # pick the /dev/ttyUSB0 port
```

Watch the log after flashing: `WiFi Connected` plus the pad's IP address confirm it's online, and from then on it's reachable as `deepdeck.local` (mDNS). If your router doesn't resolve mDNS, use the IP from the flash log (or your router's client list) as `host` in step 3 — ideally with a DHCP reservation so it stays put.

**Wrong WiFi credentials?** There is no fallback hotspot/captive portal — fix `secrets.yaml` and flash again over USB. Once the pad is on WiFi, every later update works without the cable: `uvx esphome run firmware/deepdeck.yaml --device deepdeck.local`. After that first flash the pad can live anywhere on your network, powered by any USB charger.

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
| **Press** a key | OLED shows `key N`, the project name, model + effort (e.g. `fable-5 xhigh`) and context usage (`ctx 17%`) for 3 s |
| **Double-press** (<400 ms) | Focuses that session's window on your desktop (VS Code windows match best) |
| **Hold** (≥600 ms) | Pick up the session — the key blinks; **click another key** to move/swap it there; same key or 10 s cancels |
| **Left knob turn** | Key-LED brightness, 5 % steps (1 % steps below 10 %, down to a barely-glowing 1 % night mode; persists across reboots) — or scrolls the menu while it's open |
| **Left knob press** (while a name overlay shows) | Opens the session's context menu: **restart session** (reloads its VS Code window — the conversation survives and comes back hook-tracked), **toggle remote** (types `/remote-control` for you; the key flips blue/white instantly) and **compact context** (types `/compact` — the phone app has no manual compact, the deck does). Turn to choose, press to execute; any key or 8 s cancels |
| **Right knob turn** | Toggles what the notification LEDs show: **usage** (traffic light for the 5-hour limit) or **manual** (your fixed color) |
| **Right knob press** | Notification-LED settings: first press = brightness, second = color (11 saturated presets incl. off), third closes. Turn to adjust with live preview; values survive reboots; 8 s idle closes too |

After a menu action the key's overlay stays up and shows the real result — `reloading`, `remote on`/`off`, `compacting`, or `locked` / `failed` / `no window` when nothing happened. The two **notification LEDs** (next to the USB port) have two modes: **usage** shows the 5-hour limit as a traffic light (green relaxed, amber past 75 %, red past 90 %, dark when unknown), **manual** shows your chosen color unconditionally. Their brightness is independent of the keys, and 0 % means completely off in both modes. A session within reach of auto-compaction (context ≥ 85 %) shows `!` behind its context percentage on the OLED and in the CLI.

All menu actions work by typing into your desktop (wmctrl focus + xdotool palette injection) — they need an **unlocked** X session. On a locked screen the daemon refuses (the keystrokes would go to the screen locker, not to VS Code) and logs `desktop is locked` instead of pretending success.

The OLED idles on the account usage screen: `5h` (current session window) and `7d` (weekly) with progress bars and reset times. The daemon prefers the cache Claude Code keeps for its own `/usage` command; because that cache only refreshes on local UI activity (a session driven from the phone never touches it), the daemon falls back to fetching the same endpoint the phone app uses, authenticated with the Claude Code OAuth token already on disk — read-only, throttled to one request per 10 minutes, token sent to api.anthropic.com only. When no usage data is available at all the DeepDeck kraken takes over. Sessions keep their key for their whole lifetime, across daemon restarts and session reloads.

Session details (model, effort, context %) come from Claude Code's own transcripts — read-only, refreshed every 10 s. The context window size is inferred (200k, or 1m when the model tag, the settings default, or an observed >200k turn proves it), so a fresh low-context session on a 1m model briefly shows a conservative (too high) percentage.

Autonomous work is invisible to hooks: background subagents and their completion re-invocations never fire `UserPromptSubmit`, so hook state alone would flash green mid-run. The daemon therefore also watches the transcript and subagent files — a write newer than the last Stop flips the key back to yellow within 10 s, and the next real Stop turns it green. Green during a long autonomous run is a **signal**, not a glitch: the run has stopped and wants your attention.

## CLI

```bash
agent-monitor status           # usage limits + colored table: key, project, status, RC, model+effort, context %, duration
agent-monitor status --watch   # live view
agent-monitor test-pattern     # hardware smoke test (requires daemon stopped)
```

## Troubleshooting

- **`deepdeck.local` doesn't resolve / pad unreachable** — confirm the pad joined WiFi (LEDs/OLED alive after power-up); check your router's client list for its IP and use that as `host` in `config.toml`; make sure PC and pad are on the same LAN (guest WiFi networks often isolate clients). The daemon reconnects automatically and logs `pad connection failed` once per outage.
- **A key never changes from white although the session is active** — that session started before the hooks existed. Reload its VS Code window (`Ctrl+Shift+P` → *Developer: Reload Window*); the conversation is kept, tracking starts immediately.
- **`pad firmware mismatch` in `journalctl --user -u agent-monitor`** — reflash OTA and restart the daemon (see version coupling above).
- **A menu action shows `locked` / `failed` / `no window`** — that is the daemon's honest result on the key's overlay: `locked` = unlock the PC (the injection cannot reach VS Code through a screen locker), `no window` = no window matching that project, `failed` = see `journalctl --user -u agent-monitor` for the reason.
- **Double-press does nothing** — is `wmctrl` installed? Does `systemctl --user show-environment` contain `DISPLAY`? Check the journal for `focus request` lines.
- **More than 16 sessions** — extras get no key (overflow) but stay visible in the CLI; real sessions evict blue scanned entries when keys run short.
- **A knob does nothing, or turns the wrong way** — encoder wiring varies between builds. Left knob lives on GPIO25/26 (button 34), right knob on GPIO32/33 (button 27) in `firmware/deepdeck.yaml`: swap the two pin *pairs* if your knobs are exchanged, or swap `pin_a`/`pin_b` within one knob if its direction is inverted; reflash OTA.
- **Remote-control OFF doesn't change the color** (when toggled *inside* the session) — `/remote-control off` leaves the relay socket open and no external trace, so the detector only catches it after a session restart. Toggling **via the pad menu** is exact and instant: the daemon initiated it, so it pins the known state itself until the session restarts.
- **The usage screen shows `--%`** — that limit's reset time passed, Claude Code's cache is outdated, *and* the live fallback fetch is failing (machine offline, or the OAuth token expired because no Claude Code process renewed it). The daemon refuses to display a number it knows is outdated; it retries and the bar returns by itself.
- **A key doesn't turn red although the session is waiting** — red has three sources: (1) the `Notification` hook (terminal CLI only — the VS Code extension never fires it for inline permission dialogs, verified 2026-08); (2) the `PermissionRequest`/`PermissionDenied` hooks, which the VS Code extension **does** fire (bench-verified on 2.1.226) — sessions must be **reloaded once** after `install-hooks.py` so they pick the events up; (3) transcript detection for **questions and plan approvals** (`AskUserQuestion`/`ExitPlanMode`), which needs no hooks at all and works everywhere with ≤ 10 s delay. If permission prompts stay yellow: the session probably started before the hooks were installed (reload it), or your Claude Code version predates the permission-hook events — questions still turn red via (3). `journalctl --user -u agent-monitor | grep -i permission` shows the events arriving.

## Development

```bash
uv run pytest -q                                # 191 tests
uvx esphome config firmware/deepdeck.yaml       # firmware schema check
uvx esphome compile firmware/deepdeck.yaml      # full build
```

Design documents live in `docs/superpowers/` — the spec records every decision and hardware finding, including why the key matrix needs a custom scan (the PCB has no pull resistors; the firmware replicates the stock firmware's weak-drive-contention method).

## License

[MIT](LICENSE)

## Acknowledgments

- [DeepSea Developments](https://www.deepseadev.com/) for the open-source DeepDeck — pin mappings and the key-scan method were derived from their [stock firmware](https://github.com/DeepSea-Developments/DeepDeck.Ahuyama.fw), and the kraken artwork comes from their bottom-plate design.
- Built with [ESPHome](https://esphome.io/), [aioesphomeapi](https://github.com/esphome/aioesphomeapi), and [Claude Code](https://claude.com/claude-code).
