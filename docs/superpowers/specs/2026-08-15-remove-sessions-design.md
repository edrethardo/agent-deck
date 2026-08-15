# Removing sessions from the deck (WB-42)

**Date:** 2026-08-15 · **Ticket:** WB-42 · **Project:** agent-monitor / agent-deck

## Problem

The board fills up. Sixteen keys, and long-lived sessions that have been idle for
a day or more keep theirs for as long as their window stays open. Today a key is
freed only when its session actually ends: `SessionEnd`, a dead process caught by
the prune tick, or (for remote sessions) a probe that stops listing it. There is
no way to say "this one does not belong on my board right now", and no way to end
a session from the deck.

## Decisions

Agreed with Aaron before design:

1. **Two separate menu entries**, not one — a non-destructive `hide key` and a
   destructive `end session`.
2. **A hidden session returns on any activity** — it works, finishes, or blocks
   and it takes a key again. Hiding is a snooze, not a tombstone.
3. **`end session` stops the session's process** (SIGTERM), rather than typing
   `/exit` or closing the editor window.
4. **Internally, a hidden session stays tracked and simply loses its key**
   (approach A), because the activity-based comeback requires still watching it.

## Behaviour on the deck

The context menu (left knob press while a key's overlay shows) grows from three
entries to five, in this order:

```
> restart session
  toggle remote
  compact context
  hide key
  end session
```

The two new entries are last, so the destructive one is furthest from the entries
in daily use. Entry spacing tightens from 12 px to 10 px (y = 16, 26, 36, 46, 56)
so all five fit the 64 px panel at once; the 8 px font keeps a 2 px gap, which is
legible on this display. No scrolling, no extra menu state.

**`hide key`** takes effect immediately: the key goes dark, the overlay reports
`hidden`, and the session itself is untouched — still running, still reachable in
its editor and from the phone. Hiding works for remote sessions too, since it is
purely a display decision.

**`end session`** needs a second press. Selecting it and pressing shows
`end session? press again`; only a second press within 5 s sends the request. Any
key press or the 8 s menu timeout cancels. The confirmation lives in the firmware,
so the daemon only ever receives an already-confirmed request. The overlay then
shows `ending…`, and the key frees itself through the existing SessionEnd/prune
path — this feature adds no new deletion code.

## Internals

### Data

`Session` gains `hidden_at: float = 0.0` (0.0 = visible). It round-trips through
`to_dict`/`from_dict` like every other field, so hiding survives daemon restarts
without any extra persistence.

### Hiding

Setting `hidden_at` releases the slot (`slot = None`) and records the freed slot in
the existing per-`host:cwd` sticky-key memory, so a returning session prefers the
key it had. The renderer already ignores keyless sessions. One guard is required:
`_promote_overflow()` must skip hidden sessions, or the next promotion hands the
key straight back.

### Unhiding

`hidden_at` is cleared by whichever of these fires first — all three are signals
the daemon already collects:

| Signal | Where |
|---|---|
| A hook event arrives for that session | `apply_event` |
| Transcript activity newer than `hidden_at` | `update_context` (`info.activity`) |
| A remote probe reporting a write after `hidden_at` | `sync_remote` (`now - age`) |

On unhide the session claims a slot, preferring the remembered one, exactly like a
restarted session does today.

**Accepted consequence:** a *blocked* session (red — permission prompt, question,
usage limit) writes nothing, so hiding one keeps it hidden until something moves
it. This follows from "comes back on activity" and is the intended reading of
"I do not want this on my board"; it is the one case where hiding can conceal a
session that wants attention.

### Ending

A new daemon action sends `SIGTERM` to the session's tracked pid. It refuses, with
a spoken result on the overlay, when:

- the session is remote (`local only`) — reaching across SSH to kill a process is
  deliberately out of scope for v1;
- the pid is not a plausible local process (`failed`).

Success reports `ending…`; the session disappears when `SessionEnd` arrives or the
next prune tick notices the process is gone.

### Menu wiring

Options stay a flat index in the existing `action_req` payload: `3` = hide,
`4` = end. Unknown options are already ignored by the daemon, and the firmware only
offers what it draws, so the two sides are loosely coupled: **this change does not
alter the `set_state` arity**, and therefore does not require the lockstep
reflash-and-restart that previous updates did. Old firmware with a new daemon
simply never sends 3 or 4.

## Non-goals

- No auto-cleanup of stale sessions by age. If the board still feels crowded after
  living with hide, that is a separate ticket.
- No hiding from the CLI. `agent-monitor status` keeps listing every session; it
  is the complete view, the deck is the curated one. Hidden sessions are marked
  as such in the table so the state is never invisible.
- No ending remote sessions.

## Edge cases

- **Hidden session dies** — pruned normally; hiding does not affect liveness.
- **All keys occupied when a hidden session returns** — it takes a free key, else
  it waits in overflow, exactly like any other session.
- **Hide the session you are looking at** — allowed; the overlay reports `hidden`
  and closes.
- **Daemon restart** — `hidden_at` persists; sessions that were active meanwhile
  unhide on their first activity signal.
- **SIGTERM refused or the process already gone** — reported as `failed`; the
  prune tick cleans up if it really did exit.

## Testing

- **Registry:** hide releases the key and remembers it; each of the three unhide
  signals restores it; overflow promotion skips hidden sessions; pruning is
  unaffected by hidden state.
- **Render:** a hidden session lights no LED and occupies no key name.
- **Daemon:** options 3 and 4 dispatch correctly; ending a remote session refuses;
  the result notes (`hidden`, `ending…`, `local only`, `failed`) reach the pad.
- **Actions:** end sends the signal to the right pid and refuses an implausible one.
- **CLI:** hidden sessions appear in the table marked as hidden.
- **Firmware:** `esphome config` schema check, then a bench pass with Aaron — the
  five-entry layout and the confirm press can only be judged on the hardware.
