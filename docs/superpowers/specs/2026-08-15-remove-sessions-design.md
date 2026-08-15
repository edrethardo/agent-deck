# Remove sessions from the deck — design

**Ticket:** WB-42 · **Status:** proposal, awaiting user review before implementation.

## Problem

The board fills up. Old sessions the user does not currently care about sit on keys and eventually crowd out new work — the deck holds 16 keys, and there is no way to make a session leave one short of ending the session itself. Two different needs are in play:

- *"Get this off the deck, but don't touch it."* The session is fine, just uninteresting right now — the user still wants it reachable in VS Code and on the phone.
- *"I'm done with this session."* Actually close it.

## Decisions (locked with the user, 2026-08-15)

1. Both actions ship. Two new menu entries: **hide key** and **end session**.
2. A hidden session comes back **on any activity** (any transcript write or hook event).
3. "End" means **SIGTERM to the claude process**, not palette injection. Local sessions only in v1.
4. Internally, hiding sets a **`hidden_at` timestamp on the session** — the session is still tracked, just without a slot and unrendered.

## Deck-side design

### Menu

The context menu (left knob press on a selected key) grows from three entries to five, in this order:

```
> restart session
  toggle remote
  compact context
  hide key
  end session
```

The two new entries live at the bottom, farthest from the ones the user uses most, with the destructive one last. The knob still cycles mod-n and clockwise/anticlockwise still step forward/back; only the modulus changes (from 3 to 5).

**OLED layout.** The current firmware draws entries at 12-pixel spacing (y = 16, 28, 40), which runs off the bottom of the 64-pixel panel at five entries. Change to 10-pixel spacing (y = 16, 26, 36, 46, 56). The font is 8 pixels tall; gaps go from 4 px to 2 px — still comfortably legible on this panel, and the ">" cursor character is unchanged. The header row at y = 0 is untouched.

### `hide key`

One press acts immediately.

- The daemon sets `hidden_at = now` on the session and refreshes the display.
- The key goes dark; the overlay note briefly reads `hidden` and then clears with the usual 3-second window.
- The session itself is untouched: still in VS Code, still on the phone, still tracked in `state.json` (with `hidden_at` persisted, so a daemon restart preserves the hidden state).
- **Works for remote sessions too** — hiding is purely a display decision, not something we do to the remote host.

**Comeback rule.** A hidden session is unhidden as soon as `activity > hidden_at`, where *activity* is the same signal the daemon already uses to keep sessions yellow during autonomous runs: newest mtime of the transcript plus its `subagents/agent-*.jsonl` files, refreshed on the 10-second `_ctx_loop`. Hook events count as activity too (any `apply_event` for a hidden session unhides it). Result: the session reclaims a key the moment it does anything at all, so hide behaves like a snooze the user can't accidentally leave a stuck session inside.

### `end session`

Requires a **second press within 5 seconds**. This is the only destructive menu action; a mis-press must not close a live conversation.

- First press: the daemon records `end_arm = (slot, now)`; the overlay note reads `end session? press again`.
- Any other key press, a different menu action, or the 8-second menu timeout clears the arm.
- Confirming second press: the daemon sends `SIGTERM` to the session's process (`sess.pid`) via `os.kill`. The overlay reads `ending…`. The key frees itself within a few seconds via the `SessionEnd` hook or the next prune tick.
- Refuses for **remote sessions** with the overlay note `local only` — the probe is read-only and v1 does not reach across SSH to kill things.
- Refuses if the desktop is **locked**, same rule as the palette-injection actions — even though `kill` doesn't need X, refusing keeps the "locked = no destructive changes" invariant honest.

### Firmware ↔ daemon protocol

The pad publishes menu actions on `action_req` as the string `"<slot>:<option>:<ms>"`. Two new option numbers:

- `3` → hide
- `4` → end (second press if armed, else arms)

The daemon dispatches on option, so this is purely an added entry in the mapping — no protocol change to `set_state`.

## Registry changes

Extend `Session` with:

```python
hidden_at: float | None = None   # None = visible; a timestamp otherwise
```

Persisted in `to_dict`/`from_dict` like the other fields.

Extend `SessionRegistry`:

- **`hide(slot, now)`** — set `hidden_at` on the session at that slot, free its `slot` (set to `None`), remember the freed slot via `_remember_slot`, and return `True` if anything changed. Overflow promotion runs so a waiting session gets the key.
- **`_unhide(sess)`** — private helper: clear `hidden_at`, claim a slot via `_claim_slot_for_real` (overflow if none free). Returns `True` if the session became visible.
- **`update_context`** — after adopting the new info, walk every hidden session: if `info.activity > sess.hidden_at`, call `_unhide(sess)`. This is the "on any activity" comeback.
- **`apply_event`** — for any event on a hidden session, call `_unhide(sess)` **before** `status_for_event` runs. Any hook activity is enough to bring the key back; the event's own status transition then applies as usual.

The two callers ensure both signals — file-based (autonomous work) and hook-based (real events) — bring a hidden session back within one tick.

Rendering treats a hidden session as absent: `led_colors`, `key_names`, `overlay_info` all skip sessions where `hidden_at is not None or slot is None`. The CLI status table adds a `hidden` row style so `agent-monitor status` still shows them (dimmed, no key number), so the user can see what's tucked away.

## Daemon changes

- `action_slot` gains handlers for options 3 and 4.
  - Option 3 → `registry.hide(slot, now)` → refresh → `_note(slot, "hidden")`.
  - Option 4 → check `_end_arm.get(slot)`; if unarmed or older than 5 s, arm and `_note(slot, "end session? press again")` (7-second note); if armed, call `_end_session(sess)`.
- `_end_session(sess)` refuses for `sess.host` (remote) with `_note(slot, "local only")`; refuses if `actions.display_locked()` (consistent with other destructive actions) with `_note(slot, "locked")`; otherwise `os.kill(sess.pid, signal.SIGTERM)` in a `to_thread`, `_note(slot, "ending…")`, and lets `SessionEnd`/prune reap the entry.
- The end-arm dict lives on the daemon (not the registry), so it never touches persisted state. It clears on any pad event other than an option-4 confirm and on the menu timeout.

## What stays out of v1 (YAGNI)

- **No hide for the CLI/API.** The pad menu is the trigger. If the user wants a keyboard shortcut, a `agent-monitor hide <key>` subcommand is a small follow-up.
- **No "unhide via pad."** A hidden session comes back on activity; there's nothing to trigger. If a user hides a truly quiet session, they can prod it (open the VS Code window and type) to bring it back, or restart the daemon.
- **No remote end.** Ending a session on `spheron-AI-PC` from the deck would need the probe to gain a *write* path, which the entire remote-sessions design deliberately refused.
- **No confirm dialog for hide.** It's non-destructive, and the OLED already shows the state.

## Testing

Unit-level (TDD, no hardware):

- `test_hide_takes_the_slot_away_and_keeps_the_session`
- `test_hidden_session_returns_on_activity`
- `test_hidden_session_returns_on_any_hook_event`
- `test_hide_persists_across_daemon_restart`
- `test_end_arms_on_first_press_and_kills_on_second`
- `test_end_arm_clears_on_menu_timeout_or_other_action`
- `test_end_refuses_for_remote_sessions`
- `test_end_refuses_when_display_locked`
- `test_render_and_status_skip_hidden_sessions`
- `test_cli_status_lists_hidden_sessions_dimmed`

Hardware bench-check after OTA flash and daemon restart (documented, not automated):

1. Select a chatty session → menu → *hide key* → key goes dark, session keeps running in VS Code.
2. Type in that session → key returns within ~10 s.
3. Select an idle session → *end session* → confirm on second press → key frees, VS Code window closes cleanly.
4. Select a session on `spheron-AI-PC` → *end session* → note reads `local only`, session unharmed.
5. Lock the desktop → *end session* → refuses with `locked`.

## Firmware/daemon coupling

Adding options 3/4 doesn't change the `set_state` argument list, so old-daemon-with-new-firmware and vice versa keep working: an unknown option is silently ignored on the daemon side (safe), and old firmware simply cannot emit options 3/4 (also safe). The firmware still needs an OTA flash for the 5-entry menu itself; no `pad firmware mismatch` will trigger.

## Rollback

The change touches menu wiring and one new field on `Session`. Reverting is a straightforward `git revert` of the feature commit(s); persisted `hidden_at` values in `state.json` are ignored by an older `from_dict` (`bool(d.get(..., False))` pattern is stable). The daemon does not need any special reset step.
