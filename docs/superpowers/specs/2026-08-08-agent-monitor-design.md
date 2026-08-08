# Agent Monitor — Design

**Date:** 2026-08-08
**Status:** Design approved by user

## Problem

Aaron runs several Claude Code sessions in parallel (terminal and VSCode, different projects) and cannot see which session is currently working, finished, or waiting for input. He wants a traffic-light display per session:

- 🟢 **Green** — available: session is open and waiting for a (new) prompt; also right after finishing a task.
- 🟡 **Yellow** — busy: Claude is working.
- 🔴 **Red** — needs input: Claude is blocked *mid-task* and needs Aaron (permission prompt, question, plan approval). Explicitly **not** red: finished and waiting for the next prompt — that is green.

## Decisions (from brainstorming)

1. **Hardware display:** DeepDeck (open-source ESP32 macropad by DeepSea Developments). Specs: ESP32-WROOM-32D, WiFi, 18× SK6812 RGB LEDs (16 keys), OLED 0.96" SSD1306 128×64 (I2C), 2× EC11 encoders, CP2102N USB serial. Source: https://deepdeck.co/en/QuickStartGuide/hw-specs/
2. **One key/LED per session** (no aggregate) — up to 16 sessions.
3. **Red only when blocked** (see above).
4. **Display:** DeepDeck **plus** a terminal view (CLI).
5. **Firmware may be replaced** — the macropad functionality is not needed.
6. **Chosen approach:** ESPHome firmware on the pad + ESPHome **native API** from the PC (no MQTT broker). Rejected: ESPHome+MQTT (a broker is extra infrastructure with no current benefit), forking the stock firmware (much more effort, unnecessary since the firmware is replaceable).

## Architecture

```
Claude sessions ──hooks──▶ agent-monitor daemon ──ESPHome API (WiFi)──▶ DeepDeck
 (terminal, VSCode)            │        │                                16 LEDs + OLED
                               │        └── state.json ◀── agent-monitor status (CLI)
                               └── systemd user service
```

Claude Code hooks report status changes to a local daemon. The daemon keeps the state of all sessions, pushes it to the DeepDeck, and writes it to a state file the CLI reads from. The pad is a dumb display with no logic of its own.

## Components

One Python package `agent-monitor` (Python 3.12, `uv`) with three entry points, plus an ESPHome configuration.

### 1. ESPHome firmware (`firmware/deepdeck.yaml`)

- Base: `esp32` (WROOM-32D), WiFi, `api` (with encryption), `ota`.
- 18 SK6812 LEDs as an addressable strip (`esp32_rmt_led_strip`), SSD1306 OLED over I2C.
- A user-defined API service receives the complete display state: LED colors (array) + OLED text lines. No logic on the ESP32.
- Pin mapping (LED data pin, I2C pins) taken from the open stock firmware: https://github.com/DeepSea-Developments/DeepDeck.Ahuyama.fw *(to be extracted during implementation)*.

### 2. Daemon (`agent-monitor daemon`)

- Runs as a systemd user service.
- Listens on a Unix socket (under `$XDG_RUNTIME_DIR/agent-monitor/`) for hook events.
- State per session: `session_id`, status, project path (`cwd`), PID of the Claude process, slot (key 1–16), timestamp of the last event.
- Slot assignment: new session → lowest free slot; stays stable for the lifetime of the session.
- On every state change: update LEDs + OLED via `aioesphomeapi` and write `state.json` atomically.

### 3. Hook client (`agent-monitor hook`)

- Invoked by Claude Code on `SessionStart`, `UserPromptSubmit`, `Notification`, `Stop`, `SessionEnd`; registered globally in `~/.claude/settings.json` (applies to all projects).
- Reads the hook JSON from stdin, adds the PID of the Claude process (parent PID of the hook process), sends everything to the socket.
- Fire-and-forget with a short timeout; all errors are swallowed. **The hook must never block Claude or produce errors** — even when the daemon is not running.

### 4. CLI (`agent-monitor status [--watch]`)

- Reads `state.json`, shows a colored table: slot, project (directory name), status, time in current status.
- `--watch`: live refresh.
- Clearly reports when the daemon is not running.

Additionally: `agent-monitor test-pattern` cycles all LEDs through green/yellow/red once (hardware smoke test).

## Status logic

| Hook event | New status |
|---|---|
| `SessionStart` | 🟢 green |
| `UserPromptSubmit` | 🟡 yellow |
| `Notification` (permission/question) | 🔴 red |
| `Stop` | 🟢 green |
| `SessionEnd` | Session removed, slot freed, LED off |

- `Notification` events are filtered by message text: permission requests/questions → red; pure idle notifications ("waiting for your input" after sitting idle at the prompt) do **not** change the status, so unused sessions stay green. *The exact payload texts will be verified with real sessions during implementation.*
- Unknown/irrelevant events are ignored.

## OLED display

One text line per active session: `<slot> <project name> <status symbol>`. At most 8 lines (display height); further sessions are cut off — the LEDs still show all 16 slots.

## Error handling

- **Daemon not running:** hooks silently drop their event; CLI reports "daemon is not running".
- **Pad offline / WiFi gone:** daemon reconnects indefinitely (`aioesphomeapi` reconnect logic) and pushes the *complete* state after every reconnect, not deltas. No stale LED state.
- **Session killed hard** (`SessionEnd` missing): daemon periodically (~every 15 s) checks the Claude PIDs via `/proc`; dead sessions are removed.
- **Daemon restart:** state is loaded from `state.json` and immediately cleaned up via PID check.
- **More than 16 sessions:** overflow sessions get no LED but are visible in the CLI.

## Testing

- **Unit tests (TDD):** state machine (event → status transition), slot assignment/release, staleness cleanup, notification filtering, hook payload parsing.
- **Daemon tests:** against a fake pad (mocked `aioesphomeapi`), incl. reconnect-pushes-full-state.
- **Hardware smoke test:** ESPHome config compiles; `agent-monitor test-pattern` on the real pad.

## Process-scan discovery + UNKNOWN status (added 2026-08-09 on Aaron's request)

Sessions started before the hooks were installed never fire hook events, so they were invisible. The daemon therefore periodically (~20 s) scans `/proc` for running `claude` processes (top-most match per process tree, deduplicated against sessions already known by PID), reads each one's cwd, and registers it as a synthetic session (`session_id = "proc-<pid>"`) with the new status 🔵 **UNKNOWN** (blue LED, `?` on the OLED, "unknown" in the CLI) — visible, but honestly marked as "status not tracked". Scanned sessions are pruned by the existing PID liveness check. If a real hook event ever arrives for the same PID, the synthetic session hands its slot to the real one.

Additionally the hook client walks its process ancestry to record the PID of the actual `claude` process (not an intermediate shell/wrapper), fixing sessions being wrongly pruned when the hook's immediate parent is short-lived (observed with the VS Code extension).

## Key-press name overlay (added 2026-08-08 on Aaron's request)

Pressing a key shows that session's name on the OLED for ~3 seconds, then the list returns. Mechanics: the daemon sends a third array `names: string[]` (16 entries, full project name per slot, empty = free) with every `set_state` call; the firmware scans the 4×4 key matrix (rows GPIO 0/4/5/12, columns GPIO 16/15/14/13, from the stock firmware) and on key press displays `key N` plus the stored name (or `(free)`), refreshing the display immediately.

## Deliberately not in v1

- Beyond the name overlay, pad keys trigger nothing on the PC (e.g. "press key → focus that session's terminal" would be a later feature).
- No encoder functions, no battery-operation tuning.
- No GUI beyond the CLI, no MQTT, no Home Assistant.

## Open points for implementation

1. Extract pin mapping (LED data pin, I2C) from the stock firmware.
2. Verify exact `Notification` payload texts with real sessions.
3. WiFi credentials and API key: locally in `firmware/secrets.yaml` (not committed; `.gitignore`).
