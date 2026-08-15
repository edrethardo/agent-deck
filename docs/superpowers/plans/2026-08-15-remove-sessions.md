# Removing sessions from the deck (WB-42) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the pad menu take a session off the deck without touching it (`hide key`), and stop a session outright (`end session`), so a full board can be curated.

**Architecture:** A hidden session stays in the registry and simply loses its key: a new `Session.hidden_at` timestamp, a slot release, and a guard so overflow promotion skips it. It returns on the first sign of activity — a hook event, a transcript write newer than `hidden_at`, or a remote probe age — all signals the daemon already collects. Ending a session sends `SIGTERM` to the pid the registry already tracks; the existing SessionEnd/prune path does the removal. The firmware grows two menu entries and owns the confirm press for the destructive one.

**Tech Stack:** Python 3.14, pytest (asyncio_mode=auto), ESPHome/C++ lambdas, uv.

**Spec:** `docs/superpowers/specs/2026-08-15-remove-sessions-design.md`

---

## File Structure

| File | Responsibility in this change |
|---|---|
| `src/agent_monitor/model.py` | `Session.hidden_at` field + round-trip |
| `src/agent_monitor/state.py` | `hide_slot()`, unhide on the three activity signals, promotion guard |
| `src/agent_monitor/actions.py` | `end_session()` — SIGTERM a local pid |
| `src/agent_monitor/daemon.py` | menu options 3/4, result notes, remote refusal |
| `src/agent_monitor/statusview.py` | mark hidden sessions in the CLI table |
| `firmware/deepdeck.yaml` | five menu entries, 10 px spacing, confirm press |
| `tests/test_model.py`, `test_state.py`, `test_actions.py`, `test_daemon.py`, `test_statusview.py` | the tests for each of the above |

`render.py` needs **no** change: `led_colors`, `key_names` and `overlay_info` already skip sessions whose `slot is None` (see `render.py:47,92,117`). Task 3 proves that with a test rather than assuming it.

---

### Task 1: `hidden_at` on the session model

**Files:**
- Modify: `src/agent_monitor/model.py:20-40` (dataclass fields), `to_dict`, `from_dict`
- Test: `tests/test_model.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_model.py`:

```python
def test_session_hidden_at_defaults_and_roundtrips():
    sess = Session("a", "/p", 1, Status.AVAILABLE, 0, 1.0)
    assert sess.hidden_at == 0.0

    sess.hidden_at = 1234.5
    assert Session.from_dict(sess.to_dict()).hidden_at == 1234.5
    assert Session.from_dict({"session_id": "b", "status": "busy"}).hidden_at == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_model.py::test_session_hidden_at_defaults_and_roundtrips -v`
Expected: FAIL — `AttributeError: 'Session' object has no attribute 'hidden_at'`

- [ ] **Step 3: Write minimal implementation**

In `src/agent_monitor/model.py`, add the field after `host`:

```python
    hidden_at: float = 0.0  # when the user took this session off the deck; it
    #                         keeps running and returns on its next activity
```

In `to_dict()` add:

```python
            "hidden_at": self.hidden_at,
```

In `from_dict()` add:

```python
            hidden_at=float(d.get("hidden_at", 0.0)),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_model.py -q`
Expected: PASS, all tests in the file green.

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/model.py tests/test_model.py
git commit -m "feat: Session.hidden_at field"
```

---

### Task 2: Hiding releases the key and remembers it

**Files:**
- Modify: `src/agent_monitor/state.py` (new `hide_slot`, guard in `_promote_overflow` at line 334)
- Test: `tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_state.py` (`_start` already exists at the top of that file):

```python
def test_hide_slot_frees_the_key_but_keeps_the_session():
    reg = SessionRegistry()
    _start(reg, "a", pid=5)
    assert reg.hide_slot(0, now=100.0) is True
    (s,) = reg.sessions()
    assert s.slot is None
    assert s.hidden_at == 100.0
    assert s.session_id == "a"          # still tracked, still alive


def test_hide_slot_on_an_empty_key_is_a_no_op():
    reg = SessionRegistry()
    assert reg.hide_slot(3, now=100.0) is False


def test_hidden_session_is_not_promoted_into_a_free_key():
    reg = SessionRegistry()
    _start(reg, "a", pid=5)
    reg.hide_slot(0, now=100.0)
    reg._promote_overflow()
    assert reg.sessions()[0].slot is None


def test_a_hidden_session_is_still_pruned_when_its_process_dies():
    reg = SessionRegistry()
    _start(reg, "a", pid=5)
    reg.hide_slot(0, now=100.0)
    assert reg.prune(lambda pid: False) is True
    assert reg.sessions() == []


def test_hidden_session_prefers_its_old_key_when_it_returns():
    reg = SessionRegistry()
    _start(reg, "a", t=1.0, pid=5)      # slot 0
    _start(reg, "b", t=1.0, pid=6)      # slot 1
    reg.hide_slot(0, now=100.0)
    _start(reg, "c", t=2.0, pid=7)      # takes the free slot 0? no: memory holds it
    reg.unhide(reg.by_id("a"), now=200.0)
    assert reg.by_id("a").slot == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py -k "hide" -v`
Expected: FAIL — `AttributeError: 'SessionRegistry' object has no attribute 'hide_slot'`

- [ ] **Step 3: Write minimal implementation**

In `src/agent_monitor/state.py`, add these two methods (put them next to `swap_slots`, around line 135):

```python
    def hide_slot(self, slot: int, now: float) -> bool:
        """Take the session on `slot` off the deck without touching it.

        The session keeps running and stays tracked; it simply loses its key
        until its next sign of activity (see `unhide`)."""
        for sess in self._sessions.values():
            if sess.slot == slot:
                self._remember_slot(sess)   # prefer this key when it returns
                sess.slot = None
                sess.hidden_at = now
                _LOGGER.info("hid session %s (was slot %s)", sess.session_id[:8], slot)
                return True
        return False

    def unhide(self, sess: Session, now: float) -> bool:
        """Put a hidden session back on the board. True if anything changed."""
        if not sess.hidden_at:
            return False
        sess.hidden_at = 0.0
        key = self._slot_key(sess.host, sess.cwd)
        preferred = self._last_slot_by_cwd.get(key)
        if preferred is not None and self._slot_is_free(preferred):
            sess.slot = preferred
            self._last_slot_by_cwd.pop(key, None)
        else:
            sess.slot = self._claim_slot_for_real()
        _LOGGER.info("unhid session %s -> slot %s", sess.session_id[:8], sess.slot)
        return True
```

Then guard promotion — replace the loop body condition in `_promote_overflow` (line 334):

```python
    def _promote_overflow(self) -> bool:
        changed = False
        for sess in sorted(self._sessions.values(), key=lambda s: s.since):
            if sess.slot is None and not sess.hidden_at:  # hidden ones stay off
                slot = self._free_slot()
                if slot is None:
                    break
                sess.slot = slot
                changed = True
        return changed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -q`
Expected: PASS — the whole file, not just the new tests.

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/state.py tests/test_state.py
git commit -m "feat: hide a session's key without touching the session"
```

---

### Task 3: A hidden session lights nothing

**Files:**
- Test only: `tests/test_render.py` (proves `render.py` needs no change)

- [ ] **Step 1: Write the test**

Append to `tests/test_render.py` (`_sess` helper is at the top of that file):

```python
def test_hidden_session_occupies_no_key():
    sess = _sess(None)          # hiding sets slot to None
    sess.hidden_at = 100.0
    assert led_colors([sess]) == [0] * (NUM_KEY_LEDS * 3)
    assert key_names([sess]) == [""] * NUM_KEY_LEDS
    assert overlay_info([sess]) == [""] * NUM_KEY_LEDS
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_render.py::test_hidden_session_occupies_no_key -v`
Expected: PASS immediately — this test documents existing behaviour (`render.py:47,92,117` already skip `slot is None`). If it FAILS, the renderer has a real gap: fix `render.py` so keyless sessions are skipped in all three functions, then re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/test_render.py
git commit -m "test: hidden sessions occupy no key"
```

---

### Task 4: Unhide on a hook event

**Files:**
- Modify: `src/agent_monitor/state.py` — `apply_event` (line 32)
- Test: `tests/test_state.py`

- [ ] **Step 1: Write the failing test**

```python
def test_any_hook_event_unhides_the_session():
    from agent_monitor.model import Status

    reg = SessionRegistry()
    _start(reg, "a", t=1.0, pid=5)
    reg.hide_slot(0, now=100.0)
    assert reg.by_id("a").slot is None
    reg.apply_event("UserPromptSubmit", "a", "/proj/a", 5, None, 200.0)
    s = reg.by_id("a")
    assert (s.hidden_at, s.slot, s.status) == (0.0, 0, Status.BUSY)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_state.py::test_any_hook_event_unhides_the_session -v`
Expected: FAIL — `assert (100.0, None, ...) == (0.0, 0, ...)`

- [ ] **Step 3: Write minimal implementation**

In `src/agent_monitor/state.py`, `apply_event` currently ends its existing-session
branch like this (lines 77-91):

```python
        if not sess.cwd:
            # Frozen at creation otherwise: a `cd` inside the session must not
            # rename the key or shift its per-cwd sticky-slot identity.
            sess.cwd = cwd
        sess.pid = pid if pid > 1 else sess.pid
        new = status_for_event(event, message, sess.status)
        if new is None:
            return False
        finished = event == "Stop"
        if new == sess.status and finished == sess.finished:
            return False
        sess.status = new
        sess.finished = finished
        sess.since = now
        return True
```

Replace exactly that block with:

```python
        if not sess.cwd:
            # Frozen at creation otherwise: a `cd` inside the session must not
            # rename the key or shift its per-cwd sticky-slot identity.
            sess.cwd = cwd
        sess.pid = pid if pid > 1 else sess.pid
        # A hook event IS activity, so a hidden session earns its key back —
        # even for events that change nothing else (e.g. a second Stop).
        woke = self.unhide(sess, now) if sess.hidden_at else False
        new = status_for_event(event, message, sess.status)
        if new is None:
            return woke
        finished = event == "Stop"
        if new == sess.status and finished == sess.finished:
            return woke
        sess.status = new
        sess.finished = finished
        sess.since = now
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -q`
Expected: PASS. In particular `test_same_status_and_finish_state_is_no_change` must still pass — an event that changes nothing on a *visible* session still returns False.

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/state.py tests/test_state.py
git commit -m "feat: a hook event brings a hidden session back"
```

---

### Task 5: Unhide on transcript activity and on a remote probe

**Files:**
- Modify: `src/agent_monitor/state.py` — `update_context` (line 166), `sync_remote` (line 254)
- Test: `tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_transcript_activity_newer_than_the_hide_unhides():
    reg = SessionRegistry()
    _start(reg, "a", pid=5)
    reg.hide_slot(0, now=100.0)
    # a write from BEFORE the hide must not wake it
    reg.update_context({5: _ctx(activity=90.0)}, now=105.0)
    assert reg.by_id("a").slot is None
    # a write from after it does
    assert reg.update_context({5: _ctx(activity=110.0)}, now=115.0) is True
    assert reg.by_id("a").slot == 0
    assert reg.by_id("a").hidden_at == 0.0


def test_remote_probe_activity_unhides():
    reg = SessionRegistry()
    reg.sync_remote("box", [_rsess(age=5.0)], now=1000.0)
    slot = reg.sessions()[0].slot
    reg.hide_slot(slot, now=1000.0)
    # age 500 s at now=1400 -> written at 900, before the hide: stays hidden
    reg.sync_remote("box", [_rsess(age=500.0)], now=1400.0)
    assert reg.sessions()[0].slot is None
    # age 5 s at now=1500 -> written at 1495, after the hide: comes back
    reg.sync_remote("box", [_rsess(age=5.0)], now=1500.0)
    assert reg.sessions()[0].slot == slot
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py -k "unhides" -v`
Expected: FAIL — the sessions stay hidden (`assert None == 0`).

- [ ] **Step 3: Write minimal implementation**

In `update_context`, as the first thing inside the `for sess in ...` loop after
`info` is known to be non-None:

```python
            if sess.hidden_at and info.activity > sess.hidden_at:
                self.unhide(sess, now or info.activity)
                changed = True
```

In `sync_remote`, right after `sess.cwd = sess.cwd or cwd`:

```python
            if sess.hidden_at and (now - age) > sess.hidden_at:
                self.unhide(sess, now)
                changed = True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/state.py tests/test_state.py
git commit -m "feat: activity brings a hidden session back (local and remote)"
```

---

### Task 6: `end_session` sends SIGTERM

**Files:**
- Modify: `src/agent_monitor/actions.py`
- Test: `tests/test_actions.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_actions.py`:

```python
def test_end_session_signals_the_pid(monkeypatch):
    sent = []
    monkeypatch.setattr(actions.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert actions.end_session(4242) is True
    import signal
    assert sent == [(4242, 0), (4242, signal.SIGTERM)]   # liveness probe, then stop


def test_end_session_refuses_an_implausible_pid():
    assert actions.end_session(0) is False
    assert actions.end_session(1) is False


def test_end_session_reports_a_dead_or_foreign_process(monkeypatch):
    def kill(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr(actions.os, "kill", kill)
    assert actions.end_session(4242) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_actions.py -k end_session -v`
Expected: FAIL — `AttributeError: module 'agent_monitor.actions' has no attribute 'end_session'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/agent_monitor/actions.py` (it already imports `os`):

```python
def end_session(pid: int) -> bool:
    """Stop a local session by signalling its process.

    No window, no typing, no unlocked desktop needed — but only for a live
    process of ours; the caller refuses remote sessions before getting here."""
    import signal

    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)               # exists and is ours?
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        _LOGGER.warning("cannot end session pid=%s: %s", pid, exc)
        return False
    _LOGGER.info("sent SIGTERM to session pid=%s", pid)
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_actions.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/actions.py tests/test_actions.py
git commit -m "feat: end_session stops a local session's process"
```

---

### Task 7: Wire menu options 3 (hide) and 4 (end)

**Files:**
- Modify: `src/agent_monitor/daemon.py` — `_run_action` (line 120)
- Test: `tests/test_daemon.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon.py`:

```python
async def test_menu_hide_takes_the_key_and_reports_it(paths):
    state_path, sock_path = paths
    pad = FakePad()
    daemon = Daemon(SessionRegistry(), pad, state_path, sock_path,
                    time_fn=lambda: 100.0, pid_alive=lambda pid: True,
                    note_seconds=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event())            # session on slot 0
    daemon.action_slot(0, 3)
    await asyncio.sleep(0.05)
    sess = json.loads(state_path.read_text())["sessions"][0]
    assert sess["slot"] is None and sess["hidden_at"] == 100.0
    assert any(s[4][0] == "hidden" for s in pad.shows)
    await _stop(task)


async def test_menu_end_signals_and_reports(paths, monkeypatch):
    from agent_monitor import actions

    state_path, sock_path = paths
    pad = FakePad()
    ended = []
    monkeypatch.setattr(actions, "display_locked", lambda: False)
    monkeypatch.setattr(actions, "end_session", lambda pid: ended.append(pid) or True)
    daemon = Daemon(SessionRegistry(), pad, state_path, sock_path,
                    time_fn=lambda: 100.0, pid_alive=lambda pid: True,
                    note_seconds=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event(pid=4242))
    daemon.action_slot(0, 4)
    await asyncio.sleep(0.05)
    assert ended == [4242]
    assert any(s[4][0].startswith("ending") for s in pad.shows)
    await _stop(task)


async def test_menu_end_refuses_a_remote_session(paths, monkeypatch):
    from agent_monitor import actions

    state_path, sock_path = paths
    pad = FakePad()
    ended = []
    monkeypatch.setattr(actions, "display_locked", lambda: False)
    monkeypatch.setattr(actions, "end_session", lambda pid: ended.append(pid) or True)
    registry = SessionRegistry()
    registry.sync_remote("box", [{"session_id": "r1", "pid": 3024, "cwd": "/p",
                                  "age": 5.0}], now=100.0)
    daemon = Daemon(registry, pad, state_path, sock_path,
                    time_fn=lambda: 100.0, pid_alive=lambda pid: True,
                    note_seconds=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    daemon.action_slot(0, 4)
    await asyncio.sleep(0.05)
    assert ended == []
    assert any(s[4][0] == "local only" for s in pad.shows)
    await _stop(task)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_daemon.py -k "menu_" -v`
Expected: FAIL — options 3 and 4 are unknown, so `_run_action` returns without acting and no note appears.

- [ ] **Step 3: Write minimal implementation**

In `src/agent_monitor/daemon.py`, replace the body of `_run_action` up to and
including the `display_locked` check with:

```python
    async def _run_action(self, sess, option: int) -> None:
        from . import actions

        if option == 3:                       # hide: display-only, never blocked
            if self._registry.hide_slot(sess.slot, self._time_fn()):
                await self._refresh()
                await self._note(sess.slot, "hidden")
            return
        if option == 4:                       # end: stop the process itself
            if sess.host:
                _LOGGER.info("refusing to end remote session on %s", sess.host)
                await self._note(sess.slot, "local only")
                return
            ok = await asyncio.to_thread(actions.end_session, sess.pid)
            await self._note(sess.slot, "ending…" if ok else "failed")
            return

        fn = {0: actions.restart_session,
              1: actions.toggle_remote_control,
              2: actions.compact_session}.get(option)
        if fn is None:
            return
```

The rest of the method (the `display_locked` guard and the three palette actions)
stays exactly as it is — hiding and ending need no desktop at all.

**Ordering note:** `_note(sess.slot, ...)` must be called with the slot the key
still had; for hide, `hide_slot` sets `sess.slot` to None, so capture it first:

```python
        if option == 3:
            slot = sess.slot
            if self._registry.hide_slot(slot, self._time_fn()):
                await self._refresh()
                await self._note(slot, "hidden")
            return
```

Use that version.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_daemon.py -q`
Expected: PASS, including the pre-existing `test_run_action_option_mapping`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/daemon.py tests/test_daemon.py
git commit -m "feat: pad menu options for hide and end"
```

---

### Task 8: Show hidden sessions in the CLI

**Files:**
- Modify: `src/agent_monitor/statusview.py` (the `for sess in sessions:` loop)
- Test: `tests/test_statusview.py`

- [ ] **Step 1: Write the failing test**

```python
def test_hidden_session_is_listed_and_marked():
    state = {"sessions": [
        {"session_id": "a", "cwd": "/p/x", "pid": 1, "status": "available",
         "slot": None, "since": 40.0, "hidden_at": 20.0}]}
    out = render_status(state, now=100.0, daemon_up=True, width=140)
    assert "hidden" in out
    assert "x" in out          # still listed: the CLI is the complete view
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_statusview.py::test_hidden_session_is_listed_and_marked -v`
Expected: FAIL — "hidden" not in output.

- [ ] **Step 3: Write minimal implementation**

In `statusview.py`, the key cell currently reads:

```python
        slot = sess.get("slot")
        key = "—" if slot is None else str(slot + 1)
```

Change it to:

```python
        slot = sess.get("slot")
        key = "—" if slot is None else str(slot + 1)
        if sess.get("hidden_at"):
            key = "hidden"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_statusview.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/statusview.py tests/test_statusview.py
git commit -m "feat: CLI marks hidden sessions"
```

---

### Task 9: Firmware — five entries and the confirm press

**Files:**
- Modify: `firmware/deepdeck.yaml` — globals, knob rotation (lines 346, 361), knob button (line 437ff), display menu block (lines 505-507)

- [ ] **Step 1: Add a confirm global**

In the `globals:` section, next to `menu_cursor`:

```yaml
  - id: menu_confirm_until
    type: uint32_t
    restore_value: no
    initial_value: '0'
```

- [ ] **Step 2: Widen the cursor to five entries**

Line 346 (`on_clockwise`) — change `% 3` to `% 5`:

```cpp
            id(menu_cursor) = (id(menu_cursor) + 1) % 5;
```

Line 361 (`on_anticlockwise`) — one step back through five:

```cpp
            id(menu_cursor) = (id(menu_cursor) + 4) % 5;  // one step back
```

In both handlers, also clear a pending confirmation when the selection moves:

```cpp
            id(menu_confirm_until) = 0;
```

- [ ] **Step 3: Draw all five entries at 10 px spacing**

Replace lines 505-507 in the display lambda with:

```cpp
          it.print(0, 16, id(font8), id(menu_cursor) == 0 ? "> restart session" : "  restart session");
          it.print(0, 26, id(font8), id(menu_cursor) == 1 ? "> toggle remote" : "  toggle remote");
          it.print(0, 36, id(font8), id(menu_cursor) == 2 ? "> compact context" : "  compact context");
          it.print(0, 46, id(font8), id(menu_cursor) == 3 ? "> hide key" : "  hide key");
          if (id(menu_cursor) == 4 && (int32_t) (millis() - id(menu_confirm_until)) < 0) {
            it.print(0, 56, id(font8), "> end session? press again");
          } else {
            it.print(0, 56, id(font8), id(menu_cursor) == 4 ? "> end session" : "  end session");
          }
```

- [ ] **Step 4: Require a second press for entry 4**

In the knob button `on_press` lambda (line ~437), replace the publish block inside
`if (id(menu_slot) >= 0) { ... }` with:

```cpp
              if (id(menu_cursor) == 4 && (int32_t) (now - id(menu_confirm_until)) >= 0) {
                // first press on the destructive entry only arms it
                id(menu_confirm_until) = now + 5000;
                id(menu_until) = now + 8000;
                id(oled).update();
                return;
              }
              ESP_LOGI("menu", "action slot=%d option=%d", id(menu_slot), id(menu_cursor));
              id(action_req).publish_state(to_string(id(menu_slot)) + ":" +
                                           to_string(id(menu_cursor)) + ":" + to_string(now));
              id(menu_confirm_until) = 0;
              // Reopen the key's overlay instead of a blind "sent" toast — the
              // daemon pushes the real result (hidden/ending…/failed) into the
              // overlay's info lines within a second.
              id(overlay_slot) = id(menu_slot);
              id(overlay_until) = now + 4000;
              id(menu_slot) = -1;
```

- [ ] **Step 5: Clear the arming when the menu closes**

In the display lambda, where the menu times out (`id(menu_slot) = -1;` after the
`menu_until` check, line ~510) add:

```cpp
        id(menu_confirm_until) = 0;
```

and do the same in the key-matrix scan where a key press closes the menu
(line 270: `if (id(menu_slot) >= 0) { id(menu_slot) = -1; }`):

```cpp
                if (id(menu_slot) >= 0) { id(menu_slot) = -1; id(menu_confirm_until) = 0; }
```

- [ ] **Step 6: Schema check and build**

Run: `uvx esphome config firmware/deepdeck.yaml >/dev/null && echo CONFIG-OK`
Expected: `CONFIG-OK`

Run: `uvx esphome compile firmware/deepdeck.yaml 2>&1 | tail -2`
Expected: `INFO Successfully compiled program.`

- [ ] **Step 7: Commit**

```bash
git add firmware/deepdeck.yaml
git commit -m "feat: five-entry pad menu with a confirm press for end session"
```

---

### Task 10: Full suite, deploy, bench-verify with Aaron

**Files:** none modified — this is the verification task.

- [ ] **Step 1: Run the whole suite**

Run: `uv run pytest -q`
Expected: all tests pass (240 before this plan, plus the ~15 added here).

- [ ] **Step 2: Flash and restart**

```bash
uvx esphome upload firmware/deepdeck.yaml --device deepdeck.local
systemctl --user restart agent-monitor
```

Expected: `INFO OTA successful`. The `set_state` arity is unchanged by this plan,
so the two sides are not lockstep-coupled — an old daemon simply never receives
options 3/4.

- [ ] **Step 3: Bench pass with Aaron**

Ask him to check, on the hardware:
1. All five entries visible and readable at 10 px spacing.
2. `hide key` → key goes dark, overlay says `hidden`, the session keeps running.
3. `agent-monitor status` still lists it, marked `hidden`.
4. Typing in that session (or its next Stop) brings the key back — same key if it
   was still free.
5. `end session` shows `end session? press again`, cancels on any other key, and
   on the second press the session ends and the key frees within seconds.
6. `end session` on the `@spheron-AI-PC` key reports `local only` and does nothing.

- [ ] **Step 4: Update the README**

Add the two entries to the gesture table's context-menu row, and document hide's
comeback rule in the "Using the deck" prose. Bump the test count in
`## Development`.

- [ ] **Step 5: Commit and push**

```bash
git add -A
git commit -m "docs: hide key and end session on the deck"
git push
```

---

## Amendments made during execution

Recorded because the plan was wrong in two places and the reviews caught both:

1. **Task 2 — key reservation was wrong.** The plan's test demanded a hidden session get its old key back even after a new session had claimed it, which forced an implementation that reserved hidden sessions' keys. That defeats the feature: hiding is meant to FREE a key, and reserved-but-invisible keys would push new sessions into overflow. Replaced with two tests encoding best effort: `test_hidden_session_reclaims_its_old_key_when_it_is_still_free` and `test_hiding_really_frees_the_key_for_a_new_session`.
2. **Task 7 — an assertion that could never pass.** `assert s[4][0] == "local only"` cannot hold for a remote session, because `overlay_info` always prepends the `@host` badge; the real value is `"local only\n@box"`. Changed to `.startswith(...)`, matching the file's existing convention.

Two hardenings were added beyond the plan, both from review findings:

3. **`end_session` verifies its target** (`scan.is_claude_process`): registry pids can be up to 15 s stale, and signalling a recycled pid could have hit an unrelated process. It now re-reads `/proc/<pid>/cmdline` and refuses anything that is not currently a claude process — the `os.kill(pid, 0)` probe became redundant and was dropped.
4. **The armed confirmation string was one character too wide** for the 128 px panel (26 chars against a 25-char budget), so `"> end session? press again"` became `"> end session? confirm"`.

Two small cleanups: `unhide` lost its unused `now` parameter, and the hide branch lost a redundant `_refresh()` (its `_note` already pushes the freed key).
