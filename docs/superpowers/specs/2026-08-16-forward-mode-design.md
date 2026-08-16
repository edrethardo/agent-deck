# Forward mode (WB-162)

**Date:** 2026-08-16 · **Ticket:** WB-162 · **Project:** agent-monitor / agent-deck

## Problem

The board has sixteen keys and they fill up. Today a session that arrives when
every key is taken gets no key at all: it waits in overflow, invisible on the
pad, no matter how urgently it needs attention. Meanwhile a session that has sat
idle for thirty hours keeps its key purely because it got there first. The deck
stops being a status display exactly when it matters most.

## Decisions

Agreed with Aaron before design:

1. **Displacement happens only when the board is full.** While a free key exists,
   nothing is reshuffled — keys stay where you learned them.
2. **The settings menu opens on a left-knob press with no key overlay showing**
   (today a no-op). It holds `forward mode` for now and has room for more.
3. Sessions that are **working** or **need action** are the ones that displace;
   the victim is the session idle longest.

## Behaviour

When a session needs a key, has none, and no key is free:

- if forward mode is **off** → it waits in overflow (today's behaviour, unchanged);
- if forward mode is **on** → it takes the key of the **session idle longest**,
  which moves to overflow.

"Needs a key" means the session is `BUSY` (working) or `WAITING` (blocked on you).
It applies both to a session arriving on a full board and to one already in
overflow that starts working or becomes blocked.

"Idle longest" means: status `AVAILABLE`, largest age of `since` (the timestamp
of its last state change). Sessions that are `BUSY`, `WAITING` or hidden are never
victims. **Green needs no special rule:** a green key is one that just finished, so
its `since` is recent by construction and it cannot be the longest-idle candidate —
a waiting result never gets bumped off the board.

The displaced session is not lost. Overflow already means "tracked, listed in the
CLI, no key": it keeps its remembered key in the sticky-key memory, returns
automatically when a key frees, and — if it becomes active itself — comes back by
the same rule, displacing whatever is idlest then. Under pressure the board
self-sorts toward what is alive.

**Hidden sessions are never resurrected by forward mode.** Hiding is an explicit
user decision (WB-42); only activity undoes it.

## The settings menu

Pressing the left knob while no key overlay is showing opens a global menu:

```
settings
> forward mode: on
```

Turning scrolls (one entry today, the structure allows more), pressing toggles the
value in place, and the menu closes on any key press or after 8 s — matching the
per-key context menu's behaviour. The per-key context menu and the right knob's
notification-LED settings are untouched.

## Where the setting lives

**The pad owns it.** The global is declared `restore_value: yes`, so the toggle
survives a pad reboot, and the firmware publishes the current value on every
change through a new `setting_req` text sensor as `forward:<0|1>:<millis>`.

The daemon adopts what the pad reports. This has two consequences worth stating:

- The value the menu shows is always the value in effect — there is no second copy
  on the PC that could drift from it.
- A **replayed** state on (re)connect must be ACCEPTED, unlike the focus/move/action
  channels where a replay would re-fire a stale key press. A setting is state, not an
  event, so `pad.py`'s 1 s replay-suppression window must not apply to this sensor —
  that is how a restarted daemon recovers the current value.

The `set_state` action arity does not change, so this feature needs no lockstep
flash-and-restart: an old daemon simply never hears about the new sensor.

## CLI

`agent-monitor status` prints `forward mode: on` next to the usage line when it is
enabled, so a setting that silently rearranges keys is never invisible. Nothing is
printed when it is off.

## Non-goals

- No continuous sorting. The board is only touched when a key is genuinely needed.
- No per-session priority or pinning. If that is wanted later it is its own ticket.
- No displacement by merely *finished* (green) sessions — only `BUSY`/`WAITING`
  sessions displace, since green means "read me", not "working".

## Edge cases

- **Board full and every session is active** — no eligible victim exists, so the
  newcomer waits in overflow exactly as it does today.
- **Two sessions equally idle** — deterministic tie-break: the one whose `since` is
  older wins, and on an exact tie the one that appears later in slot order, so the
  choice is stable rather than dependent on dict iteration.
- **The victim is the session the user is looking at** — accepted: the key changing
  is the feedback, and the CLI still lists it.
- **Forward mode toggled off** — no un-shuffling; the board is left as it is and
  simply stops displacing from then on.
- **Pad offline** — the daemon keeps the last value it heard; when the pad returns,
  its replayed state re-establishes the truth.

## Testing

- **Registry:** displacement picks the longest-idle `AVAILABLE` session; never a
  `BUSY`/`WAITING`/hidden one; does nothing when a free key exists; does nothing
  when forward mode is off; the victim keeps its remembered key; an overflow session
  that becomes `BUSY` claims a key; a green session is not chosen while an older
  idle one exists.
- **Daemon:** a `setting_req` payload flips the registry flag and refreshes; an
  unknown setting name is ignored.
- **Pad:** the settings sensor is subscribed, and a replayed value on reconnect is
  accepted (the inverse of the existing replay-suppression test).
- **CLI:** the marker appears only when enabled.
- **Firmware:** `esphome config` + `compile`, then a bench pass with Aaron — the new
  gesture and the menu can only be judged on the hardware.
