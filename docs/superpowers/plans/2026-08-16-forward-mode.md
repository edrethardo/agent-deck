# Forward Mode Implementation Plan (WB-162)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When every key on the macropad is taken, let a session that is working or blocked on the user take the key of the session that has been idle longest — switchable from a new settings menu on the pad.

**Architecture:** One registry flag (`forward_mode`) and one new fallback inside the existing slot-allocation path (`_claim_slot_for_real`), which today already evicts a scan-discovered session when the board is full. The pad owns the setting: a `restore_value: yes` global in the firmware, published through a new `setting_request` text sensor, adopted by the daemon. Because a setting is state rather than an event, this is the one pad channel whose replayed value on reconnect must be accepted.

**Tech Stack:** Python 3.14, pytest (asyncio_mode=auto), aioesphomeapi, ESPHome/C++ lambdas, uv.

**Spec:** `docs/superpowers/specs/2026-08-16-forward-mode-design.md`

---

## File Structure

| File | Responsibility in this change |
|---|---|
| `src/agent_monitor/state.py` | `forward_mode` flag, the displacement fallback, claiming a key when an overflow session becomes active |
| `src/agent_monitor/pad.py` | subscribe the new `setting_request` sensor; accept its replayed value |
| `src/agent_monitor/daemon.py` | `set_setting` entry point, wire the pad callback, persist the flag in state.json |
| `src/agent_monitor/cli.py` | pass the new callback through |
| `src/agent_monitor/statusview.py` | print `forward mode: on` when enabled |
| `firmware/deepdeck.yaml` | settings menu on left-press-with-no-overlay, the toggle, the new text sensor |
| `tests/test_state.py`, `test_pad.py`, `test_daemon.py`, `test_statusview.py` | the tests for each |

`render.py` is untouched: displacement only changes `slot` values, which it already renders.

---

### Task 1: The registry flag and the displacement rule

**Files:**
- Modify: `src/agent_monitor/state.py` (`__init__`, `_claim_slot_for_real` at line 356, `to_dict`/`from_dict` at 387-401)
- Test: `tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_state.py` (helpers `_start` and `MAX_SLOTS` are already imported there):

```python
def _fill_board(reg, status_of=None):
    """One session per key, each idler than the last: s0 is the longest idle."""
    for i in range(MAX_SLOTS):
        _start(reg, f"s{i}", t=float(i), pid=100 + i)
        if status_of and i in status_of:
            reg.apply_event(status_of[i], f"s{i}", f"/proj/s{i}", 100 + i, None, float(i))
    return reg


def test_forward_mode_is_off_by_default_and_round_trips():
    reg = SessionRegistry()
    assert reg.forward_mode is False
    reg.forward_mode = True
    assert SessionRegistry.from_dict(reg.to_dict()).forward_mode is True
    assert SessionRegistry.from_dict({"sessions": []}).forward_mode is False


def test_full_board_without_forward_mode_leaves_the_newcomer_in_overflow():
    reg = _fill_board(SessionRegistry())
    _start(reg, "new", t=99.0, pid=999)
    assert reg.by_id("new").slot is None
    assert reg.by_id("s0").slot == 0          # the idlest keeps its key


def test_forward_mode_gives_the_longest_idle_key_to_a_working_session():
    reg = _fill_board(SessionRegistry())
    reg.forward_mode = True
    _start(reg, "new", t=99.0, pid=999)       # SessionStart -> AVAILABLE, no displacing
    assert reg.by_id("new").slot is None
    # it starts working: now it earns a key
    reg.apply_event("UserPromptSubmit", "new", "/proj/new", 999, None, 100.0)
    assert reg.by_id("new").slot == 0         # took the longest-idle session's key
    assert reg.by_id("s0").slot is None       # which moved to overflow


def test_forward_mode_never_displaces_an_active_or_hidden_session():
    from agent_monitor.model import Status

    # every key busy except s5, which is hidden -> no eligible victim
    reg = SessionRegistry()
    for i in range(MAX_SLOTS):
        _start(reg, f"s{i}", t=float(i), pid=100 + i)
        reg.apply_event("UserPromptSubmit", f"s{i}", f"/proj/s{i}", 100 + i, None, float(i))
    reg.forward_mode = True
    reg.hide_slot(5, now=50.0)                # frees key 5...
    _start(reg, "filler", t=60.0, pid=900)    # ...which this takes
    _start(reg, "new", t=99.0, pid=999)
    reg.apply_event("UserPromptSubmit", "new", "/proj/new", 999, None, 100.0)
    assert reg.by_id("new").slot is None      # all keys busy: nobody is displaced
    assert all(s.status is Status.BUSY or s.session_id in ("filler", "new")
               for s in reg.sessions() if s.slot is not None)


def test_a_blocked_session_also_displaces():
    reg = _fill_board(SessionRegistry())
    reg.forward_mode = True
    _start(reg, "new", t=99.0, pid=999)
    reg.apply_event("PermissionRequest", "new", "/proj/new", 999, None, 100.0)
    assert reg.by_id("new").slot == 0


def test_the_displaced_session_keeps_its_key_in_memory():
    reg = _fill_board(SessionRegistry())
    reg.forward_mode = True
    _start(reg, "new", t=99.0, pid=999)
    reg.apply_event("UserPromptSubmit", "new", "/proj/new", 999, None, 100.0)
    victim = reg.by_id("s0")
    assert reg._last_slot_by_cwd.get("/proj/s0") == 0   # remembered for its return


def test_an_overflow_session_takes_a_key_only_when_it_turns_active():
    reg = _fill_board(SessionRegistry())
    reg.forward_mode = True
    _start(reg, "new", t=99.0, pid=999)
    assert reg.by_id("new").slot is None          # idle: waits
    reg.apply_event("Stop", "new", "/proj/new", 999, None, 100.0)
    assert reg.by_id("new").slot is None          # finished/green: still waits
    reg.apply_event("UserPromptSubmit", "new", "/proj/new", 999, None, 101.0)
    assert reg.by_id("new").slot == 0             # working: claims a key


def test_a_scanned_session_is_evicted_before_any_idle_one():
    """The existing UNKNOWN fallback stays the first resort: a scan-discovered
    session never had hooks, so it is the cheapest thing on the board to lose."""
    reg = SessionRegistry()
    for i in range(MAX_SLOTS - 1):
        _start(reg, f"s{i}", t=float(i), pid=100 + i)
    reg.add_scanned(500, "/proj/scanned", 50.0)   # takes the last free key
    reg.forward_mode = True
    _start(reg, "new", t=99.0, pid=999)
    reg.apply_event("UserPromptSubmit", "new", "/proj/new", 999, None, 100.0)
    assert reg.by_id("proc-500").slot is None     # the scanned one yielded
    assert reg.by_id("s0").slot == 0              # the idlest kept its key


def test_a_freshly_finished_session_is_never_the_victim():
    reg = _fill_board(SessionRegistry())
    reg.forward_mode = True
    reg.apply_event("Stop", "s0", "/proj/s0", 100, None, 90.0)   # idlest turns green
    _start(reg, "new", t=99.0, pid=999)
    reg.apply_event("UserPromptSubmit", "new", "/proj/new", 999, None, 100.0)
    assert reg.by_id("s0").slot == 0              # green: `since` is recent
    assert reg.by_id("s1").slot is None           # the next-idlest yielded instead


def test_an_exact_tie_is_broken_deterministically():
    reg = SessionRegistry()
    for i in range(MAX_SLOTS):
        _start(reg, f"s{i}", t=5.0, pid=100 + i)  # every session equally idle
    reg.forward_mode = True
    _start(reg, "new", t=5.0, pid=999)
    reg.apply_event("UserPromptSubmit", "new", "/proj/new", 999, None, 5.0)
    assert reg.by_id(f"s{MAX_SLOTS - 1}").slot is None   # highest slot yields
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py -k "forward or displac" -v`
Expected: FAIL — `AttributeError: 'SessionRegistry' object has no attribute 'forward_mode'`

- [ ] **Step 3: Write minimal implementation**

In `SessionRegistry.__init__`, after `self._last_slot_by_cwd: dict[str, int] = {}`:

```python
        # When the board is full, let a working/blocked session take the key of
        # the session idle longest. Owned by the pad; see docs/superpowers/specs.
        self.forward_mode = False
```

Replace `_claim_slot_for_real` (line 356) with:

```python
    def _claim_slot_for_real(self) -> int | None:
        """Free slot, or evict the newest scanned (UNKNOWN) session to overflow.

        With forward mode on, a full board additionally yields the key of the
        session idle longest — see `_displace_longest_idle`."""
        slot = self._free_slot()
        if slot is not None:
            return slot
        unknowns = [s for s in self._sessions.values()
                    if s.status is Status.UNKNOWN and s.slot is not None]
        if unknowns:
            victim = max(unknowns, key=lambda s: s.since)
            slot, victim.slot = victim.slot, None
            return slot
        return self._displace_longest_idle()

    def _displace_longest_idle(self) -> int | None:
        """Free the key of the longest-idle session, or None if there is none.

        Only plain idle sessions are eligible: a BUSY or WAITING one is doing
        or needing something, and a hidden one holds no key anyway. A green
        (recently finished) session is protected for free, because `since` is
        recent by construction and so it is never the longest idle."""
        if not self.forward_mode:
            return None
        idle = [s for s in self._sessions.values()
                if s.slot is not None and s.status is Status.AVAILABLE]
        if not idle:
            return None
        # oldest `since` wins; the higher slot breaks an exact tie so the
        # choice never depends on dict iteration order
        victim = min(idle, key=lambda s: (s.since, -s.slot))
        self._remember_slot(victim)          # it comes back to this key
        slot, victim.slot = victim.slot, None
        _LOGGER.info("forward mode: session %s yields slot %s (idle longest)",
                     victim.session_id[:8], slot)
        return slot
```

In `to_dict` (line 387) add the flag:

```python
    def to_dict(self) -> dict:
        return {"sessions": [s.to_dict() for s in self.sessions()],
                "forward_mode": self.forward_mode}
```

In `from_dict`, before `reg._normalize_slots()`:

```python
        reg.forward_mode = bool(data.get("forward_mode", False))
```

Finally, an overflowed session must be able to CLAIM a key the moment it turns
active — otherwise the rule above can never fire for it. In `apply_event`, the
existing-session branch currently ends:

```python
        sess.status = new
        sess.finished = finished
        sess.since = now
        return True
```

Replace with:

```python
        sess.status = new
        sess.finished = finished
        sess.since = now
        if sess.slot is None and not sess.hidden_at and new in (Status.BUSY, Status.WAITING):
            # It is doing something or needs the user: with forward mode on it
            # may now displace the idlest session (a no-op when a key is free
            # anyway, and when forward mode is off).
            sess.slot = self._claim_slot_for_real()
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -q`
Expected: PASS — the whole file. `test_seventeenth_session_overflows_then_gets_freed_slot` must still pass, since forward mode is off by default and that test never enables it.

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/state.py tests/test_state.py
git commit -m "feat: forward mode gives a full board's idlest key to an active session"
```

---

### Task 2: The pad's settings channel

**Files:**
- Modify: `src/agent_monitor/pad.py` (`__init__`, the entity-subscription block around line 93, `_handle_state` around line 130)
- Test: `tests/test_pad.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_pad.py`, the `FakeClient.list_entities_services` returns a list of entities; add the new sensor to it by changing that list to include `SimpleNamespace(object_id="setting_request", key=96)` alongside the existing three. Then append:

```python
async def test_setting_event_triggers_the_setting_callback(fakes):
    created, factory = fakes
    seen = []
    pad = DeepDeckPad(_cfg(), client_factory=factory,
                      on_setting=lambda name, value: seen.append((name, value)))
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await asyncio.sleep(1.1)
    created[0].state_callback(SimpleNamespace(key=96, state="forward:1:123"))
    assert seen == [("forward", True)]
    created[0].state_callback(SimpleNamespace(key=96, state="forward:0:456"))
    assert seen == [("forward", True), ("forward", False)]
    task.cancel()


async def test_a_replayed_setting_is_accepted_unlike_a_replayed_press(fakes):
    """A setting is state, not an event: the value replayed on (re)connect is
    how a restarted daemon learns what the pad currently holds."""
    created, factory = fakes
    seen = []
    focused = []
    pad = DeepDeckPad(_cfg(), client_factory=factory, on_focus=focused.append,
                      on_setting=lambda name, value: seen.append((name, value)))
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    # both arrive immediately after subscribe, i.e. inside the replay window
    created[0].state_callback(SimpleNamespace(key=99, state="7:555"))
    created[0].state_callback(SimpleNamespace(key=96, state="forward:1:123"))
    assert focused == []                       # a replayed key press is ignored
    assert seen == [("forward", True)]         # a replayed setting is adopted
    task.cancel()


async def test_an_unparsable_setting_is_ignored(fakes):
    created, factory = fakes
    seen = []
    pad = DeepDeckPad(_cfg(), client_factory=factory,
                      on_setting=lambda name, value: seen.append((name, value)))
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await asyncio.sleep(1.1)
    created[0].state_callback(SimpleNamespace(key=96, state="garbage"))
    assert seen == []
    task.cancel()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pad.py -k setting -v`
Expected: FAIL — `TypeError: DeepDeckPad.__init__() got an unexpected keyword argument 'on_setting'`

- [ ] **Step 3: Write minimal implementation**

In `DeepDeckPad.__init__`, add the parameter after `on_action`:

```python
        on_setting: Callable[[str, bool], None] | None = None,
```

and store it next to the others:

```python
        self._on_setting = on_setting
```

In the entity-subscription block, extend the condition that decides whether to subscribe at all, and map the new object id:

```python
                if (self._on_focus is not None or self._on_move is not None
                        or self._on_action is not None or self._on_setting is not None):
                    keys = {}
                    for e in entities:
                        oid = getattr(e, "object_id", "")
                        if oid == "focus_request":
                            keys[e.key] = "focus"
                        elif oid == "move_request":
                            keys[e.key] = "move"
                        elif oid == "action_request":
                            keys[e.key] = "action"
                        elif oid == "setting_request":
                            keys[e.key] = "setting"
```

In `_handle_state`, the replay guard currently returns for every kind. A setting must survive it, so handle settings BEFORE that guard — insert this immediately after the `self._last_payloads[state.key] = payload` line and before the `if self._loop_time() - subscribed_at < 1.0:` block:

```python
        if kind == "setting":
            # A setting is state, not an event: the value replayed on connect
            # is exactly how a restarted daemon learns what the pad holds, so
            # this channel deliberately skips the replay suppression below.
            parts = payload.split(":")
            if len(parts) >= 2 and self._on_setting is not None:
                self._on_setting(parts[0], parts[1] == "1")
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pad.py -q`
Expected: PASS, including the existing `test_replayed_state_right_after_connect_is_ignored`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/pad.py tests/test_pad.py
git commit -m "feat: pad reports settings; replayed values are adopted"
```

---

### Task 3: The daemon adopts the setting and persists it

**Files:**
- Modify: `src/agent_monitor/daemon.py` (new `set_setting`, the `_refresh` payload at line 266), `src/agent_monitor/cli.py` (wire the callback)
- Test: `tests/test_daemon.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_setting_from_the_pad_flips_forward_mode_and_persists(paths):
    state_path, sock_path = paths
    pad = FakePad()
    registry = SessionRegistry()
    daemon = Daemon(registry, pad, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    daemon.set_setting("forward", True)
    await asyncio.sleep(0.05)
    assert registry.forward_mode is True
    assert json.loads(state_path.read_text())["forward_mode"] is True
    daemon.set_setting("forward", False)
    await asyncio.sleep(0.05)
    assert json.loads(state_path.read_text())["forward_mode"] is False
    await _stop(task)


async def test_an_unknown_setting_name_is_ignored(paths):
    state_path, sock_path = paths
    registry = SessionRegistry()
    daemon = Daemon(registry, None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    daemon.set_setting("teleport", True)
    await asyncio.sleep(0.05)
    assert registry.forward_mode is False
    await _stop(task)


async def test_forward_mode_survives_a_daemon_restart(paths):
    state_path, sock_path = paths
    state_path.write_text(json.dumps({"sessions": [], "forward_mode": True}))
    registry = SessionRegistry()
    daemon = Daemon(registry, None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    assert json.loads(state_path.read_text())["forward_mode"] is True
    await _stop(task)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_daemon.py -k setting -v`
Expected: FAIL — `AttributeError: 'Daemon' object has no attribute 'set_setting'`

- [ ] **Step 3: Write minimal implementation**

In `daemon.py`, add next to `action_slot`:

```python
    def set_setting(self, name: str, value: bool) -> None:
        """Adopt a setting the pad reports (it owns them; we follow)."""
        if name != "forward":
            _LOGGER.info("ignoring unknown pad setting %r", name)
            return
        if self._registry.forward_mode != value:
            self._registry.forward_mode = value
            _LOGGER.info("forward mode %s", "on" if value else "off")
        asyncio.get_event_loop().create_task(self._refresh())
```

In `_refresh`, add the flag to the payload:

```python
            payload = {
                "updated": self._time_fn(),
                "sessions": [s.to_dict() for s in sessions],
                "usage": [dataclasses.asdict(lim) for lim in self._usage],
                "forward_mode": self._registry.forward_mode,
            }
```

In `cli.py`'s `_run_daemon`, add the callback to the `DeepDeckPad(...)` construction, which currently reads `on_focus=daemon.focus_slot, on_move=daemon.move_slot, on_action=daemon.action_slot`:

```python
        pad = DeepDeckPad(
            cfg, on_focus=daemon.focus_slot, on_move=daemon.move_slot,
            on_action=daemon.action_slot, on_setting=daemon.set_setting,
        ) if cfg else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/daemon.py src/agent_monitor/cli.py tests/test_daemon.py
git commit -m "feat: daemon adopts and persists the pad's forward-mode setting"
```

---

### Task 4: Show it in the CLI

**Files:**
- Modify: `src/agent_monitor/statusview.py` (the usage-line block before the table)
- Test: `tests/test_statusview.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_forward_mode_marker_appears_only_when_enabled():
    on = {"sessions": [{"session_id": "a", "cwd": "/p/x", "pid": 1,
                        "status": "busy", "slot": 0, "since": 40.0}],
          "forward_mode": True}
    assert "forward mode: on" in render_status(on, now=100.0, daemon_up=True, width=140)
    off = dict(on, forward_mode=False)
    assert "forward mode" not in render_status(off, now=100.0, daemon_up=True, width=140)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_statusview.py::test_forward_mode_marker_appears_only_when_enabled -v`
Expected: FAIL — "forward mode: on" not in output.

- [ ] **Step 3: Write minimal implementation**

In `render_status`, immediately after the `if usage:` block that prints the usage line and before `sessions = (state or {}).get("sessions", [])`:

```python
    if (state or {}).get("forward_mode"):
        # a setting that silently rearranges keys must not be invisible
        console.print("[cyan]forward mode: on[/cyan] — a working session takes "
                      "the longest-idle key when the board is full")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_statusview.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/statusview.py tests/test_statusview.py
git commit -m "feat: CLI shows when forward mode is on"
```

---

### Task 5: The firmware settings menu

**Files:**
- Modify: `firmware/deepdeck.yaml` (globals, `text_sensor:`, the left knob's rotation and button lambdas, the display lambda, the key-matrix scan)

Read each region before editing; the descriptions below are from memory and the file is authoritative.

- [ ] **Step 1: Add the globals and the sensor**

In `globals:`, next to `menu_confirm_until`:

```yaml
  - id: gmenu_open
    type: bool
    restore_value: no
    initial_value: 'false'
  - id: gmenu_until
    type: uint32_t
    restore_value: no
    initial_value: '0'
  - id: forward_mode
    type: bool
    restore_value: yes      # the pad owns this setting; it survives reboots
    initial_value: 'false'
```

In the `text_sensor:` block, next to `action_req`:

```yaml
  - platform: template
    id: setting_req
    name: "Setting Request"
    update_interval: never
```

- [ ] **Step 2: Publish the current value on boot**

So a daemon that (re)connects learns the pad's value, publish once at startup. In the `esphome:` `on_boot:` `then:` list, after the existing `light.turn_on` actions:

```yaml
      - lambda: |-
          id(setting_req).publish_state(std::string("forward:") +
                                        (id(forward_mode) ? "1" : "0") + ":0");
```

- [ ] **Step 3: Open the settings menu on a left-knob press with no overlay**

In the left knob button's `on_press` lambda, the chain currently ends with the `else if` that opens the per-key menu from an active overlay. Add a final `else` for the case where nothing is showing:

```cpp
            } else if (id(overlay_slot) >= 0 && (int32_t) (now - id(overlay_until)) < 0) {
              id(menu_slot) = id(overlay_slot);
              id(menu_cursor) = 0;
              id(menu_until) = now + 8000;
              id(overlay_slot) = -1;
            } else if (id(gmenu_open)) {
              // second press inside the settings menu toggles the entry
              id(forward_mode) = !id(forward_mode);
              id(setting_req).publish_state(std::string("forward:") +
                                            (id(forward_mode) ? "1" : "0") +
                                            ":" + to_string(now));
              id(gmenu_until) = now + 8000;
            } else {
              id(gmenu_open) = true;          // nothing showing: global settings
              id(gmenu_until) = now + 8000;
            }
```

- [ ] **Step 4: Draw the settings menu**

In the display lambda, BEFORE the `if (id(menu_slot) >= 0) {` block (so the per-key menu keeps priority), add:

```cpp
      if (id(gmenu_open)) {
        if ((int32_t) (millis() - id(gmenu_until)) < 0) {
          it.print(0, 0, id(font8), "settings");
          it.print(0, 20, id(font8), id(forward_mode) ? "> forward mode: on"
                                                      : "> forward mode: off");
          it.print(0, 50, id(font8), "press: toggle");
          return;
        }
        id(gmenu_open) = false;   // timed out
      }
```

- [ ] **Step 5: Close it on any key press**

In the 25 ms key-matrix scan lambda, the line that closes the per-key menu currently reads
`if (id(menu_slot) >= 0) { id(menu_slot) = -1; id(menu_confirm_until) = 0; }`. Extend it:

```cpp
                if (id(menu_slot) >= 0) { id(menu_slot) = -1; id(menu_confirm_until) = 0; }
                id(gmenu_open) = false;
```

- [ ] **Step 6: Verify**

Run: `uvx esphome config firmware/deepdeck.yaml >/dev/null && echo CONFIG-OK`
Expected: `CONFIG-OK`

Run: `cd /home/aaron/code/agent_monitor && uvx esphome compile firmware/deepdeck.yaml 2>&1 | tail -3`
Expected: `INFO Successfully compiled program.` (several minutes; do not interrupt)

Run: `uv run pytest -q` — must still pass; the firmware change cannot affect it, so this only guards against an accidental Python edit.

- [ ] **Step 7: Commit**

```bash
git add firmware/deepdeck.yaml
git commit -m "feat: settings menu on the left knob with a forward-mode toggle"
```

---

### Task 6: Deploy and bench-verify with Aaron

**Files:** none — verification.

- [ ] **Step 1: Full suite**

Run: `uv run pytest -q`. Expected: every test passes (265 before this plan, plus the ~14 added here).

- [ ] **Step 2: Flash and restart**

```bash
uvx esphome upload firmware/deepdeck.yaml --device deepdeck.local
systemctl --user restart agent-monitor
```

Expected: `INFO OTA successful`. The `set_state` arity is unchanged, so the two sides are not lockstep-coupled.

- [ ] **Step 3: Bench checks with Aaron**

1. Press the left knob with no key overlay showing → `settings` / `forward mode: off` appears.
2. Press again → it flips to `on`; `agent-monitor status` shows the forward-mode line within a second.
3. Any key press closes the menu; the per-key context menu (press a key first, then the knob) still works unchanged.
4. Restart the daemon (`systemctl --user restart agent-monitor`) → the CLI still shows forward mode on, proving the pad re-taught it on reconnect.
5. Power-cycle the pad → the menu still shows `on`, proving `restore_value`.
6. With a full board and forward mode on, start a new session and give it a prompt → it appears on the longest-idle session's key, and that session vanishes from the deck but stays in the CLI.

- [ ] **Step 4: README**

Add the settings-menu gesture to the gesture table and one paragraph describing forward mode under "Using the deck". Bump the test count in `## Development`.

- [ ] **Step 5: Commit and push**

```bash
git add -A
git commit -m "docs: forward mode on the deck"
git push
```
