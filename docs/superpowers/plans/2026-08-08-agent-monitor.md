# Agent Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Traffic-light display (green/yellow/red) for all local Claude Code sessions on a DeepDeck macropad (ESPHome) plus a terminal CLI.

**Architecture:** Claude Code hooks send events to a local daemon (Unix socket). The daemon keeps a session registry (status, slot 0–15, PID), renders LED colors + OLED lines from it and pushes them to the pad via the ESPHome native API; in parallel it writes `state.json` for the CLI. The pad is a dumb display (ESPHome firmware with a `set_state` service).

**Tech Stack:** Python 3.14 (raised from 3.12 during review — asyncio's `serve_forever` cancellation only closes client connections since 3.14, older versions hang on shutdown), `uv`, `aioesphomeapi>=45,<46`, `rich`, pytest + pytest-asyncio (`asyncio_mode=auto`), ESPHome (via `uvx`), systemd user service.

**Spec:** `docs/superpowers/specs/2026-08-08-agent-monitor-design.md`

**Language:** All code, comments, docstrings, UI strings, and commit messages in English.

**Hardware facts** (extracted from the stock firmware `DeepSea-Developments/DeepDeck.Ahuyama.fw`):
- Key LEDs: **16× WS2812/SK6812 on GPIO17** (own strip; slot i = LED i, remap after hardware test if needed)
- Notification LEDs: 2× on GPIO23 — **unused in v1**
- OLED: SSD1306 128×64, I2C **SDA=GPIO21, SCL=GPIO22**, address 0x3C
- Board: ESP32-WROOM-32D (`esp32dev`), USB serial CP2102N

**Data formats (identical everywhere):**
- Hook→daemon (JSON line over Unix socket): `{"event": "Stop", "session_id": "...", "cwd": "/path", "pid": 12345, "message": null}`
- `state.json`: `{"updated": 1723.0, "sessions": [{"session_id": "...", "cwd": "...", "pid": 1, "status": "available", "slot": 0, "since": 1723.0}]}`
- Daemon→pad (ESPHome service `set_state`): `colors` = 48 ints (16 LEDs × RGB, 0–255), `lines` = up to 8 strings.

**File structure:**

```
pyproject.toml
.gitignore
src/agent_monitor/
  __init__.py    (empty)
  model.py       Status enum, Session dataclass, status_for_event()
  state.py       SessionRegistry (slots, apply_event, prune, (de)serialization)
  render.py      sessions → LED colors + OLED lines
  paths.py       socket/state/config paths (XDG)
  config.py      PadConfig from config.toml
  hook.py        hook client (stdin JSON → socket, never fails)
  pad.py         DeepDeckPad (aioesphomeapi, reconnect, full-state push)
  daemon.py      daemon (socket server, refresh, prune loop)
  statusview.py  CLI table (+ --watch)
  cli.py         argparse entry: daemon | hook | status | test-pattern
tests/
  test_model.py test_state.py test_render.py test_config.py
  test_hook.py test_pad.py test_daemon.py test_statusview.py
firmware/
  deepdeck.yaml
  secrets.yaml.example
systemd/agent-monitor.service
```

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/agent_monitor/__init__.py`, `tests/test_smoke.py`

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "agent-monitor"
version = "0.1.0"
description = "Traffic-light status for Claude Code sessions on a DeepDeck + CLI"
requires-python = ">=3.12"
dependencies = [
    "aioesphomeapi>=21",
    "rich>=13",
]

[project.scripts]
agent-monitor = "agent_monitor.cli:main"

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/agent_monitor"]
```

- [ ] **Step 2: Write .gitignore**

```gitignore
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
dist/
.esphome/
firmware/secrets.yaml
firmware/.esphome/
```

- [ ] **Step 3: Create package + smoke test**

`src/agent_monitor/__init__.py`: empty file.

`tests/test_smoke.py`:
```python
def test_import():
    import agent_monitor  # noqa: F401
```

- [ ] **Step 4: Sync + run tests**

Run: `uv sync && uv run pytest -q`
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore src tests uv.lock
git commit -m "chore: project scaffolding (uv, pytest, agent_monitor package)"
```

---

### Task 2: Status logic (`model.py`)

**Files:**
- Create: `src/agent_monitor/model.py`
- Test: `tests/test_model.py`

- [ ] **Step 1: Write failing tests**

`tests/test_model.py`:
```python
from agent_monitor.model import Status, status_for_event


def test_session_start_is_available():
    assert status_for_event("SessionStart", None, None) == Status.AVAILABLE


def test_user_prompt_submit_is_busy():
    assert status_for_event("UserPromptSubmit", None, Status.AVAILABLE) == Status.BUSY


def test_stop_is_available():
    assert status_for_event("Stop", None, Status.BUSY) == Status.AVAILABLE


def test_permission_notification_is_waiting():
    msg = "Claude needs your permission to use Bash"
    assert status_for_event("Notification", msg, Status.BUSY) == Status.WAITING


def test_idle_notification_keeps_current_status():
    msg = "Claude is waiting for your input"
    assert status_for_event("Notification", msg, Status.AVAILABLE) is None


def test_unknown_notification_defaults_to_waiting():
    assert status_for_event("Notification", "Something else", Status.BUSY) == Status.WAITING


def test_unknown_event_returns_none():
    assert status_for_event("PreCompact", None, Status.BUSY) is None
```

- [ ] **Step 2: Run tests — must fail**

Run: `uv run pytest tests/test_model.py -q`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'agent_monitor.model'`

- [ ] **Step 3: Implement**

`src/agent_monitor/model.py`:
```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    AVAILABLE = "available"  # green
    BUSY = "busy"            # yellow
    WAITING = "waiting"      # red


@dataclass
class Session:
    session_id: str
    cwd: str
    pid: int
    status: Status
    slot: int | None  # 0-15, None = overflow (no LED)
    since: float

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "pid": self.pid,
            "status": self.status.value,
            "slot": self.slot,
            "since": self.since,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            session_id=str(d["session_id"]),
            cwd=str(d.get("cwd", "")),
            pid=int(d.get("pid", 0)),
            status=Status(d["status"]),
            slot=d.get("slot"),
            since=float(d.get("since", 0.0)),
        )


# Notification texts that do NOT mean the session is blocked (it is just
# sitting idle at the prompt) — per spec the status stays unchanged then.
IDLE_MARKERS = ("waiting for your input",)


def status_for_event(event: str, message: str | None, current: Status | None) -> Status | None:
    """New status for a hook event; None = do not change the status."""
    if event == "SessionStart":
        return Status.AVAILABLE
    if event == "UserPromptSubmit":
        return Status.BUSY
    if event == "Stop":
        return Status.AVAILABLE
    if event == "Notification":
        text = (message or "").lower()
        if any(marker in text for marker in IDLE_MARKERS):
            return None
        return Status.WAITING
    return None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_model.py -q`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/model.py tests/test_model.py
git commit -m "feat: status logic — map hook events to traffic-light status"
```

---

### Task 3: SessionRegistry (`state.py`)

**Files:**
- Create: `src/agent_monitor/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Write failing tests**

`tests/test_state.py`:
```python
from agent_monitor.model import Status
from agent_monitor.state import SessionRegistry


def _start(reg, sid, t=1.0, pid=100):
    return reg.apply_event("SessionStart", sid, f"/proj/{sid}", pid, None, t)


def test_new_session_gets_slot_zero_and_available():
    reg = SessionRegistry()
    assert _start(reg, "a") is True
    (s,) = reg.sessions()
    assert (s.slot, s.status) == (0, Status.AVAILABLE)


def test_second_session_gets_next_slot():
    reg = SessionRegistry()
    _start(reg, "a")
    _start(reg, "b")
    assert [s.slot for s in reg.sessions()] == [0, 1]


def test_status_change_updates_since_and_reports_change():
    reg = SessionRegistry()
    _start(reg, "a", t=1.0)
    assert reg.apply_event("UserPromptSubmit", "a", "/proj/a", 100, None, 5.0) is True
    (s,) = reg.sessions()
    assert (s.status, s.since) == (Status.BUSY, 5.0)


def test_same_status_is_no_change():
    reg = SessionRegistry()
    _start(reg, "a", t=1.0)
    assert reg.apply_event("Stop", "a", "/proj/a", 100, None, 9.0) is False
    assert reg.sessions()[0].since == 1.0


def test_event_without_status_change_is_no_change():
    reg = SessionRegistry()
    _start(reg, "a")
    assert reg.apply_event("PreCompact", "a", "/proj/a", 100, None, 2.0) is False


def test_unknown_session_created_with_event_status():
    reg = SessionRegistry()
    msg = "Claude needs your permission to use Bash"
    reg.apply_event("Notification", "x", "/proj/x", 7, msg, 1.0)
    assert reg.sessions()[0].status == Status.WAITING


def test_session_end_frees_slot_for_reuse():
    reg = SessionRegistry()
    _start(reg, "a")
    _start(reg, "b")
    assert reg.apply_event("SessionEnd", "a", "/proj/a", 100, None, 2.0) is True
    _start(reg, "c")
    assert sorted(s.slot for s in reg.sessions()) == [0, 1]


def test_seventeenth_session_overflows_then_gets_freed_slot():
    reg = SessionRegistry()
    for i in range(16):
        _start(reg, f"s{i}")
    _start(reg, "overflow")
    assert reg.by_id("overflow").slot is None
    reg.apply_event("SessionEnd", "s3", "/proj/s3", 100, None, 2.0)
    assert reg.by_id("overflow").slot == 3


def test_prune_removes_dead_sessions():
    reg = SessionRegistry()
    _start(reg, "a", pid=111)
    _start(reg, "b", pid=222)
    assert reg.prune(lambda pid: pid == 222) is True
    assert [s.session_id for s in reg.sessions()] == ["b"]
    assert reg.prune(lambda pid: True) is False


def test_roundtrip_serialization():
    reg = SessionRegistry()
    _start(reg, "a")
    reg2 = SessionRegistry.from_dict(reg.to_dict())
    assert reg2.sessions()[0].session_id == "a"


def test_from_dict_tolerates_garbage():
    assert SessionRegistry.from_dict({"sessions": [{"nope": 1}]}).sessions() == []
    assert SessionRegistry.from_dict({}).sessions() == []
```

- [ ] **Step 2: Run tests — must fail**

Run: `uv run pytest tests/test_state.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_monitor.state'`

- [ ] **Step 3: Implement**

`src/agent_monitor/state.py`:
```python
from __future__ import annotations

from collections.abc import Callable

from .model import Session, Status, status_for_event

MAX_SLOTS = 16


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def sessions(self) -> list[Session]:
        """Sorted by slot, overflow (None) last."""
        return sorted(
            self._sessions.values(),
            key=lambda s: (s.slot is None, s.slot if s.slot is not None else 0, s.since),
        )

    def by_id(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def apply_event(
        self,
        event: str,
        session_id: str,
        cwd: str,
        pid: int,
        message: str | None,
        now: float,
    ) -> bool:
        """Apply an event. True if the display state changed."""
        if event == "SessionEnd":
            if self._sessions.pop(session_id, None) is None:
                return False
            self._promote_overflow()
            return True

        sess = self._sessions.get(session_id)
        if sess is None:
            status = status_for_event(event, message, None) or Status.AVAILABLE
            self._sessions[session_id] = Session(
                session_id=session_id,
                cwd=cwd,
                pid=pid,
                status=status,
                slot=self._free_slot(),
                since=now,
            )
            return True

        sess.cwd = cwd or sess.cwd
        sess.pid = pid or sess.pid
        new = status_for_event(event, message, sess.status)
        if new is None or new == sess.status:
            return False
        sess.status = new
        sess.since = now
        return True

    def prune(self, pid_alive: Callable[[int], bool]) -> bool:
        """Remove sessions whose process is dead. True if anything changed."""
        dead = [sid for sid, s in self._sessions.items() if not pid_alive(s.pid)]
        for sid in dead:
            del self._sessions[sid]
        promoted = self._promote_overflow()
        return bool(dead) or promoted

    def _free_slot(self) -> int | None:
        used = {s.slot for s in self._sessions.values() if s.slot is not None}
        for i in range(MAX_SLOTS):
            if i not in used:
                return i
        return None

    def _promote_overflow(self) -> bool:
        changed = False
        for sess in sorted(self._sessions.values(), key=lambda s: s.since):
            if sess.slot is None:
                slot = self._free_slot()
                if slot is None:
                    break
                sess.slot = slot
                changed = True
        return changed

    def to_dict(self) -> dict:
        return {"sessions": [s.to_dict() for s in self.sessions()]}

    @classmethod
    def from_dict(cls, data: dict) -> "SessionRegistry":
        reg = cls()
        for entry in data.get("sessions", []):
            try:
                sess = Session.from_dict(entry)
            except (KeyError, ValueError, TypeError):
                continue
            reg._sessions[sess.session_id] = sess
        return reg
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_state.py -q`
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/state.py tests/test_state.py
git commit -m "feat: SessionRegistry with slot assignment, overflow, and prune"
```

---

### Task 4: Rendering (`render.py`)

**Files:**
- Create: `src/agent_monitor/render.py`
- Test: `tests/test_render.py`

- [ ] **Step 1: Write failing tests**

`tests/test_render.py`:
```python
from agent_monitor.model import Session, Status
from agent_monitor.render import BRIGHTNESS, NUM_KEY_LEDS, led_colors, oled_lines


def _sess(slot, status=Status.AVAILABLE, cwd="/home/aaron/code/myproj", sid="a"):
    return Session(sid, cwd, 1, status, slot, 0.0)


def test_empty_registry_is_all_dark():
    assert led_colors([]) == [0] * (NUM_KEY_LEDS * 3)
    assert oled_lines([]) == []


def test_available_session_lights_green():
    colors = led_colors([_sess(0)])
    assert colors[0:3] == [0, int(255 * BRIGHTNESS), 0]
    assert colors[3:] == [0] * (NUM_KEY_LEDS * 3 - 3)


def test_waiting_session_lights_red_on_its_slot():
    colors = led_colors([_sess(5, Status.WAITING)])
    assert colors[15:18] == [int(255 * BRIGHTNESS), 0, 0]


def test_overflow_session_not_rendered():
    assert led_colors([_sess(None)]) == [0] * (NUM_KEY_LEDS * 3)


def test_oled_line_format():
    (line,) = oled_lines([_sess(2, Status.WAITING)])
    assert line == " 3 myproj       !"


def test_oled_truncates_to_eight_lines():
    sessions = [_sess(i, sid=f"s{i}") for i in range(10)]
    assert len(oled_lines(sessions)) == 8
```

- [ ] **Step 2: Run tests — must fail**

Run: `uv run pytest tests/test_render.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_monitor.render'`

- [ ] **Step 3: Implement**

`src/agent_monitor/render.py`:
```python
from __future__ import annotations

import os

from .model import Session, Status

NUM_KEY_LEDS = 16
# Physical LED order on the strip. If the hardware test (Task 12) reveals a
# different wiring (e.g. serpentine), remap here.
KEY_LEDS = list(range(NUM_KEY_LEDS))
BRIGHTNESS = 0.4
MAX_OLED_LINES = 8

COLORS: dict[Status, tuple[int, int, int]] = {
    Status.AVAILABLE: (0, 255, 0),
    Status.BUSY: (255, 160, 0),
    Status.WAITING: (255, 0, 0),
}
STATUS_CHAR: dict[Status, str] = {
    Status.AVAILABLE: "+",
    Status.BUSY: "~",
    Status.WAITING: "!",
}


def led_colors(sessions: list[Session]) -> list[int]:
    out = [0] * (NUM_KEY_LEDS * 3)
    for sess in sessions:
        if sess.slot is None:
            continue
        led = KEY_LEDS[sess.slot]
        r, g, b = COLORS[sess.status]
        out[led * 3 : led * 3 + 3] = [
            int(r * BRIGHTNESS),
            int(g * BRIGHTNESS),
            int(b * BRIGHTNESS),
        ]
    return out


def project_name(cwd: str) -> str:
    return os.path.basename(cwd.rstrip("/")) or cwd or "?"


def oled_lines(sessions: list[Session]) -> list[str]:
    lines = []
    for sess in sessions:
        if sess.slot is None:
            continue
        name = project_name(sess.cwd)[:12]
        lines.append(f"{sess.slot + 1:>2} {name:<12} {STATUS_CHAR[sess.status]}")
    return lines[:MAX_OLED_LINES]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_render.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/render.py tests/test_render.py
git commit -m "feat: render sessions to LED colors and OLED lines"
```

---

### Task 5: Paths and configuration (`paths.py`, `config.py`)

**Files:**
- Create: `src/agent_monitor/paths.py`, `src/agent_monitor/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

`tests/test_config.py`:
```python
from agent_monitor import paths
from agent_monitor.config import load_pad_config


def test_runtime_paths_use_xdg_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert paths.socket_path() == tmp_path / "agent-monitor" / "daemon.sock"
    assert paths.state_path() == tmp_path / "agent-monitor" / "state.json"
    assert paths.socket_path().parent.is_dir()


def test_missing_config_disables_pad(tmp_path):
    assert load_pad_config(tmp_path / "nope.toml") is None


def test_disabled_config_disables_pad(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[pad]\nenabled = false\nhost = "deepdeck.local"\n')
    assert load_pad_config(p) is None


def test_config_parses_pad_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[pad]\nenabled = true\nhost = "10.0.0.5"\napi_key = "abc"\n')
    cfg = load_pad_config(p)
    assert (cfg.host, cfg.api_key, cfg.port) == ("10.0.0.5", "abc", 6053)
```

- [ ] **Step 2: Run tests — must fail**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`src/agent_monitor/paths.py`:
```python
from __future__ import annotations

import os
from pathlib import Path


def _runtime_dir() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/agent-monitor-{os.getuid()}"))
    d = base / "agent-monitor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def socket_path() -> Path:
    return _runtime_dir() / "daemon.sock"


def state_path() -> Path:
    return _runtime_dir() / "state.json"


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "agent-monitor" / "config.toml"
```

`src/agent_monitor/config.py`:
```python
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PadConfig:
    host: str
    api_key: str = ""
    port: int = 6053
    reconnect_delay: float = 5.0


def load_pad_config(path: Path) -> PadConfig | None:
    """None if the file is missing, [pad] is missing, or enabled=false — the daemon then runs without a pad."""
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    pad = data.get("pad", {})
    if not pad.get("enabled", False) or not pad.get("host"):
        return None
    return PadConfig(
        host=str(pad["host"]),
        api_key=str(pad.get("api_key", "")),
        port=int(pad.get("port", 6053)),
        reconnect_delay=float(pad.get("reconnect_delay", 5.0)),
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/paths.py src/agent_monitor/config.py tests/test_config.py
git commit -m "feat: XDG paths and pad configuration from config.toml"
```

---

### Task 6: Hook client (`hook.py`)

**Files:**
- Create: `src/agent_monitor/hook.py`
- Test: `tests/test_hook.py`

- [ ] **Step 1: Write failing tests**

`tests/test_hook.py`:
```python
import io
import json
import socket

from agent_monitor import hook, paths


def _fake_stdin(monkeypatch, payload: str):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))


def _listen(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(paths.socket_path()))
    srv.listen(1)
    srv.settimeout(2)
    return srv


def test_hook_forwards_event(monkeypatch, tmp_path):
    srv = _listen(monkeypatch, tmp_path)
    _fake_stdin(monkeypatch, json.dumps({
        "hook_event_name": "Stop",
        "session_id": "abc",
        "cwd": "/proj",
    }))
    assert hook.main() == 0
    conn, _ = srv.accept()
    data = json.loads(conn.recv(4096).decode())
    assert data["event"] == "Stop"
    assert data["session_id"] == "abc"
    assert data["cwd"] == "/proj"
    assert isinstance(data["pid"], int) and data["pid"] > 0
    conn.close()
    srv.close()


def test_hook_forwards_notification_message(monkeypatch, tmp_path):
    srv = _listen(monkeypatch, tmp_path)
    _fake_stdin(monkeypatch, json.dumps({
        "hook_event_name": "Notification",
        "session_id": "abc",
        "cwd": "/proj",
        "message": "Claude needs your permission to use Bash",
    }))
    assert hook.main() == 0
    conn, _ = srv.accept()
    assert "permission" in json.loads(conn.recv(4096).decode())["message"]
    conn.close()
    srv.close()


def test_hook_without_daemon_still_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _fake_stdin(monkeypatch, json.dumps({"hook_event_name": "Stop", "session_id": "x"}))
    assert hook.main() == 0


def test_hook_with_garbage_stdin_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _fake_stdin(monkeypatch, "not json")
    assert hook.main() == 0
```

- [ ] **Step 2: Run tests — must fail**

Run: `uv run pytest tests/test_hook.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_monitor.hook'`

- [ ] **Step 3: Implement**

`src/agent_monitor/hook.py`:
```python
"""Hook client: invoked by Claude Code. Must NEVER fail or block —
every error is swallowed, the exit code is always 0."""

from __future__ import annotations

import json
import os
import socket
import sys

from . import paths

SEND_TIMEOUT = 0.5


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
        payload = {
            "event": data.get("hook_event_name"),
            "session_id": data.get("session_id"),
            "cwd": data.get("cwd", ""),
            "pid": os.getppid(),  # parent = the Claude process
            "message": data.get("message"),
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(SEND_TIMEOUT)
            sock.connect(str(paths.socket_path()))
            sock.sendall(json.dumps(payload).encode() + b"\n")
    except Exception:
        pass
    return 0
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_hook.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/hook.py tests/test_hook.py
git commit -m "feat: hook client — stdin JSON to daemon socket, fault-tolerant"
```

---

### Task 7: Pad client (`pad.py`)

**Files:**
- Create: `src/agent_monitor/pad.py`
- Test: `tests/test_pad.py`

**Background for the implementer:** `aioesphomeapi.APIClient` speaks the ESPHome native API. Relevant methods: `await client.connect(login=True, on_stop=cb)` (cb is an async callable `(expected_disconnect: bool) -> None`, called on connection loss), `await client.list_entities_services()` → `(entities, services)`, `client.execute_service(service, {...})`, `await client.disconnect()`. We inject a `client_factory` so tests run without a network.

> **DEVIATION (applied during implementation):** the installed aioesphomeapi 45.7.0 made `execute_service` **async**; the plan below originally assumed it was synchronous. The committed code therefore awaits its result when awaitable (`inspect.isawaitable`), the test fake's `execute_service` is `async def`, and `pyproject.toml` pins `aioesphomeapi>=45,<46`.
>
> **HARDENING (from code review, commit 4edbffc):** `run()` additionally clears `_connected`/`_client`/`_service` inside `_on_stop` (so an outage buffers instead of pushing to a dead client), creates the client inside the `try`, connects with `log_errors=False` and logs only the first failure of an outage at WARNING (rest DEBUG), uses capped exponential backoff (`min(delay*2, 60)`, reset on success), validates the `set_state` service's argument names at discovery, and bounds disconnects. `tests/test_pad.py` gained five failure-path tests. A second round (commit 123c6ea) binds each `on_stop` callback to its own connection via `_make_on_stop` (a stale callback can never tear down the live connection), uses `disconnect(force=True)` so no connection is ever abandoned alive, and floors file-loaded `reconnect_delay` at 0.5 s. The code blocks below are otherwise authoritative.

- [ ] **Step 1: Write failing tests**

`tests/test_pad.py`:
```python
import asyncio
from types import SimpleNamespace

import pytest

from agent_monitor.config import PadConfig
from agent_monitor.pad import DeepDeckPad


class FakeClient:
    def __init__(self):
        self.calls = []
        self.on_stop = None
        self.disconnected = False

    async def connect(self, login=False, on_stop=None):
        self.on_stop = on_stop

    async def list_entities_services(self):
        return [], [SimpleNamespace(name="other"), SimpleNamespace(name="set_state")]

    def execute_service(self, service, data):
        assert service.name == "set_state"
        self.calls.append(data)

    async def disconnect(self):
        self.disconnected = True


def _cfg():
    return PadConfig(host="test.local", reconnect_delay=0.01)


@pytest.fixture
def fakes():
    created = []

    def factory():
        client = FakeClient()
        created.append(client)
        return client

    return created, factory


async def test_show_after_connect_pushes_state(fakes):
    created, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await pad.show([1, 2, 3], ["line"])
    assert created[0].calls[-1] == {"colors": [1, 2, 3], "lines": ["line"]}
    task.cancel()


async def test_state_before_connect_is_pushed_on_connect(fakes):
    created, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    await pad.show([9], ["a"])
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await asyncio.sleep(0.05)
    assert created[0].calls == [{"colors": [9], "lines": ["a"]}]
    task.cancel()


async def test_reconnect_pushes_full_state_again(fakes):
    created, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await pad.show([5], ["x"])
    await created[0].on_stop(False)  # simulate connection loss
    await asyncio.sleep(0.1)
    assert len(created) >= 2
    assert created[1].calls == [{"colors": [5], "lines": ["x"]}]
    task.cancel()


async def test_show_without_connection_does_not_raise(fakes):
    _, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    await pad.show([1], [])  # run() not started — must not raise
```

- [ ] **Step 2: Run tests — must fail**

Run: `uv run pytest tests/test_pad.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_monitor.pad'`

- [ ] **Step 3: Implement**

`src/agent_monitor/pad.py`:
```python
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from aioesphomeapi import APIClient

from .config import PadConfig

_LOGGER = logging.getLogger(__name__)
SERVICE_NAME = "set_state"


class DeepDeckPad:
    """Maintains the connection to the DeepDeck and pushes the display state.

    `show()` always remembers the last state; after every (re)connect the
    complete state is pushed again — never just deltas.
    """

    def __init__(self, config: PadConfig, client_factory: Callable | None = None):
        self._cfg = config
        self._factory = client_factory or (
            lambda: APIClient(
                config.host,
                config.port,
                password=None,
                noise_psk=config.api_key or None,
            )
        )
        self._client = None
        self._service = None
        self._connected = asyncio.Event()
        self._last: tuple[list[int], list[str]] | None = None

    async def run(self) -> None:
        while True:
            stopped = asyncio.Event()

            async def _on_stop(expected_disconnect: bool) -> None:
                stopped.set()

            client = self._factory()
            try:
                await client.connect(login=True, on_stop=_on_stop)
                _, services = await client.list_entities_services()
                self._service = next(
                    (s for s in services if s.name == SERVICE_NAME), None
                )
                if self._service is None:
                    raise RuntimeError(f"service {SERVICE_NAME!r} missing on the pad")
                self._client = client
                self._connected.set()
                await self._push_last()
                await stopped.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("pad connection failed: %s", exc)
            finally:
                self._connected.clear()
                self._client = None
                try:
                    await client.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(self._cfg.reconnect_delay)

    async def wait_connected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def show(self, colors: list[int], lines: list[str]) -> None:
        self._last = (list(colors), list(lines))
        await self._push_last()

    async def _push_last(self) -> None:
        if self._client is None or self._service is None or self._last is None:
            return
        try:
            self._client.execute_service(
                self._service, {"colors": self._last[0], "lines": self._last[1]}
            )
        except Exception as exc:
            _LOGGER.warning("pad push failed: %s", exc)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_pad.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/pad.py tests/test_pad.py
git commit -m "feat: DeepDeckPad — ESPHome client with reconnect and full-state push"
```

---

### Task 8: Daemon (`daemon.py`)

> **AMENDMENTS (applied during implementation, from code review):** shutdown cancels background tasks and awaits them via `gather(..., return_exceptions=True)`; every background task gets a done-callback logging unexpected death; the prune loop survives a failing tick (log + continue); `_load_state` ignores snapshots that are valid JSON but not an object; `_handle_client` catches `ValueError` (oversized line) and wraps each `handle_line` in log-and-continue; `run()` refuses to start if another daemon is already listening on the socket (connect probe); `_refresh` is serialized by an `asyncio.Lock`; `requires-python` raised to `>=3.14`. Task 9's `_run_daemon` must install a SIGTERM handler that cancels the daemon task so systemd stops are graceful. Tests: teardown awaits cancellation, `ready.wait` is bounded, slot preservation asserted, plus four extra tests (SessionEnd, non-object snapshot, oversized line, live-socket refusal).

**Files:**
- Create: `src/agent_monitor/daemon.py`
- Test: `tests/test_daemon.py`

- [ ] **Step 1: Write failing tests**

`tests/test_daemon.py`:
```python
import asyncio
import json

import pytest

from agent_monitor.daemon import Daemon
from agent_monitor.state import SessionRegistry


class FakePad:
    def __init__(self):
        self.shows = []

    async def run(self):
        await asyncio.Event().wait()

    async def show(self, colors, lines):
        self.shows.append((colors, lines))


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "state.json", tmp_path / "daemon.sock"


async def _send(sock_path, payload: dict):
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write(json.dumps(payload).encode() + b"\n")
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)


def _event(event="SessionStart", sid="a", pid=999):
    return {"event": event, "session_id": sid, "cwd": "/proj/x", "pid": pid}


async def test_event_updates_state_file_and_pad(paths):
    state_path, sock_path = paths
    pad = FakePad()
    daemon = Daemon(SessionRegistry(), pad, state_path, sock_path,
                    time_fn=lambda: 42.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await daemon.ready.wait()
    await _send(sock_path, _event())
    state = json.loads(state_path.read_text())
    assert state["sessions"][0]["session_id"] == "a"
    assert state["sessions"][0]["status"] == "available"
    colors, lines = pad.shows[-1]
    assert colors[1] > 0  # slot 0 lights up green
    assert lines == [" 1 x            +"]
    task.cancel()


async def test_invalid_lines_are_ignored(paths):
    state_path, sock_path = paths
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await daemon.ready.wait()
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write(b"not json\n")
    writer.write(json.dumps({"event": "Stop"}).encode() + b"\n")  # no session_id
    await writer.drain()
    writer.close()
    await asyncio.sleep(0.05)
    assert json.loads(state_path.read_text())["sessions"] == []
    task.cancel()


async def test_prune_loop_removes_dead_sessions(paths):
    state_path, sock_path = paths
    alive = {"value": True}
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: alive["value"],
                    prune_interval=0.05)
    task = asyncio.create_task(daemon.run())
    await daemon.ready.wait()
    await _send(sock_path, _event())
    alive["value"] = False
    await asyncio.sleep(0.15)
    assert json.loads(state_path.read_text())["sessions"] == []
    task.cancel()


async def test_state_loaded_on_start_and_pruned(paths):
    state_path, sock_path = paths
    state_path.write_text(json.dumps({"sessions": [
        {"session_id": "old-alive", "cwd": "/p", "pid": 1, "status": "busy",
         "slot": 0, "since": 1.0},
        {"session_id": "old-dead", "cwd": "/p", "pid": 2, "status": "busy",
         "slot": 1, "since": 1.0},
    ]}))
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 2.0, pid_alive=lambda pid: pid == 1)
    task = asyncio.create_task(daemon.run())
    await daemon.ready.wait()
    sessions = json.loads(state_path.read_text())["sessions"]
    assert [s["session_id"] for s in sessions] == ["old-alive"]
    task.cancel()


async def test_stale_socket_file_is_replaced(paths):
    state_path, sock_path = paths
    sock_path.touch()
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await daemon.ready.wait()
    await _send(sock_path, _event())
    assert json.loads(state_path.read_text())["sessions"]
    task.cancel()
```

- [ ] **Step 2: Run tests — must fail**

Run: `uv run pytest tests/test_daemon.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_monitor.daemon'`

- [ ] **Step 3: Implement**

`src/agent_monitor/daemon.py`:
```python
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from .render import led_colors, oled_lines
from .state import SessionRegistry

_LOGGER = logging.getLogger(__name__)


def default_pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Daemon:
    def __init__(
        self,
        registry: SessionRegistry,
        pad,  # DeepDeckPad | None
        state_path: Path,
        socket_path: Path,
        *,
        time_fn: Callable[[], float] = time.time,
        pid_alive: Callable[[int], bool] = default_pid_alive,
        prune_interval: float = 15.0,
    ):
        self._registry = registry
        self._pad = pad
        self._state_path = state_path
        self._socket_path = socket_path
        self._time_fn = time_fn
        self._pid_alive = pid_alive
        self._prune_interval = prune_interval
        self.ready = asyncio.Event()

    async def run(self) -> None:
        self._load_state()
        self._registry.prune(self._pid_alive)
        await self._refresh()

        self._socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        tasks = [asyncio.create_task(self._prune_loop())]
        if self._pad is not None:
            tasks.append(asyncio.create_task(self._pad.run()))
        self.ready.set()
        try:
            async with server:
                await server.serve_forever()
        finally:
            for task in tasks:
                task.cancel()

    def _load_state(self) -> None:
        # Adopt the complete snapshot including slots — sessions keep their
        # key across a daemon restart.
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        self._registry = SessionRegistry.from_dict(data)

    async def _handle_client(self, reader, writer) -> None:
        try:
            while line := await reader.readline():
                await self.handle_line(line)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def handle_line(self, line: bytes) -> None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        event = data.get("event")
        session_id = data.get("session_id")
        if not event or not session_id:
            return
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        changed = self._registry.apply_event(
            event=event,
            session_id=session_id,
            cwd=data.get("cwd") or "",
            pid=pid,
            message=data.get("message"),
            now=self._time_fn(),
        )
        if changed:
            await self._refresh()

    async def _refresh(self) -> None:
        sessions = self._registry.sessions()
        payload = {
            "updated": self._time_fn(),
            "sessions": [s.to_dict() for s in sessions],
        }
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self._state_path)
        if self._pad is not None:
            await self._pad.show(led_colors(sessions), oled_lines(sessions))

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(self._prune_interval)
            if self._registry.prune(self._pid_alive):
                await self._refresh()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_daemon.py -q`
Expected: `5 passed`

- [ ] **Step 5: Run all tests**

Run: `uv run pytest -q`
Expected: `42 passed` (smoke 1 + model 7 + state 11 + render 6 + config 4 + hook 4 + pad 4 + daemon 5), 0 failed

- [ ] **Step 6: Commit**

```bash
git add src/agent_monitor/daemon.py tests/test_daemon.py
git commit -m "feat: daemon — socket server, state file, pad push, prune loop"
```

---

### Task 9: CLI (`statusview.py`, `cli.py`)

**Files:**
- Create: `src/agent_monitor/statusview.py`, `src/agent_monitor/cli.py`
- Test: `tests/test_statusview.py`

- [ ] **Step 1: Write failing tests**

`tests/test_statusview.py`:
```python
from agent_monitor.statusview import format_duration, render_status


def _state():
    return {
        "updated": 100.0,
        "sessions": [
            {"session_id": "a", "cwd": "/home/aaron/code/lead-extractor",
             "pid": 1, "status": "waiting", "slot": 0, "since": 40.0},
            {"session_id": "b", "cwd": "/home/aaron/code/graft",
             "pid": 2, "status": "busy", "slot": 1, "since": 90.0},
            {"session_id": "c", "cwd": "/home/aaron/code/over",
             "pid": 3, "status": "available", "slot": None, "since": 95.0},
        ],
    }


def test_render_contains_projects_and_status():
    out = render_status(_state(), now=100.0, daemon_up=True)
    assert "lead-extractor" in out
    assert "waiting for input" in out
    assert "working" in out
    assert "available" in out
    assert "1m0s" in out  # waiting for 60s


def test_overflow_session_shows_dash_for_key():
    out = render_status(_state(), now=100.0, daemon_up=True)
    assert "—" in out


def test_daemon_down_warning():
    out = render_status(None, now=100.0, daemon_up=False)
    assert "daemon is not running" in out


def test_empty_state_message():
    out = render_status({"updated": 1.0, "sessions": []}, now=2.0, daemon_up=True)
    assert "No active sessions" in out


def test_format_duration():
    assert format_duration(5) == "5s"
    assert format_duration(65) == "1m5s"
    assert format_duration(3725) == "1h02m"
```

- [ ] **Step 2: Run tests — must fail**

Run: `uv run pytest tests/test_statusview.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement statusview**

`src/agent_monitor/statusview.py`:
```python
from __future__ import annotations

import io
import json
import socket
import time

from rich.console import Console
from rich.table import Table

from . import paths
from .render import project_name

STATUS_LABEL = {
    "available": ("available", "green"),
    "busy": ("working", "yellow"),
    "waiting": ("waiting for input", "red bold"),
}


def format_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def daemon_running() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            sock.connect(str(paths.socket_path()))
        return True
    except OSError:
        return False


def read_state() -> dict | None:
    try:
        return json.loads(paths.state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None


def render_status(state: dict | None, now: float, daemon_up: bool) -> str:
    console = Console(file=io.StringIO(), force_terminal=False, width=80)
    if not daemon_up:
        console.print("[red]⚠ daemon is not running[/red] — start it with: "
                      "systemctl --user start agent-monitor")
    sessions = (state or {}).get("sessions", [])
    if not sessions:
        console.print("No active sessions.")
        return console.file.getvalue()

    table = Table()
    table.add_column("Key", justify="right")
    table.add_column("Project")
    table.add_column("Status")
    table.add_column("For", justify="right")
    for sess in sessions:
        label, style = STATUS_LABEL.get(sess["status"], (sess["status"], ""))
        key = "—" if sess["slot"] is None else str(sess["slot"] + 1)
        table.add_row(
            key,
            project_name(sess["cwd"]),
            f"[{style}]{label}[/{style}]" if style else label,
            format_duration(now - sess["since"]),
        )
    console.print(table)
    return console.file.getvalue()


def run_status(watch: bool) -> int:
    if not watch:
        print(render_status(read_state(), time.time(), daemon_running()), end="")
        return 0
    try:
        while True:
            out = render_status(read_state(), time.time(), daemon_running())
            print("\033[2J\033[H" + out, end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
```

- [ ] **Step 4: Implement cli.py**

`src/agent_monitor/cli.py`:
```python
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from . import paths


def _run_daemon() -> int:
    from .config import load_pad_config
    from .daemon import Daemon
    from .pad import DeepDeckPad
    from .state import SessionRegistry

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_pad_config(paths.config_path())
    pad = DeepDeckPad(cfg) if cfg else None
    if pad is None:
        logging.info("no pad configured (%s) — running without hardware", paths.config_path())
    daemon = Daemon(SessionRegistry(), pad, paths.state_path(), paths.socket_path())

    async def _main() -> None:
        # systemd stops with SIGTERM: cancel the daemon task so run()'s
        # cleanup (cancel + gather of background tasks) actually executes.
        task = asyncio.ensure_future(daemon.run())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(_main())
    except RuntimeError as exc:
        # e.g. another daemon already owns the socket — exit cleanly, non-zero
        logging.error("%s", exc)
        return 1
    return 0


async def _test_pattern_async() -> int:
    from .config import load_pad_config
    from .pad import DeepDeckPad
    from .render import NUM_KEY_LEDS

    cfg = load_pad_config(paths.config_path())
    if cfg is None:
        print(f"No pad configured — create {paths.config_path()} (see README).")
        return 1
    pad = DeepDeckPad(cfg)
    task = asyncio.create_task(pad.run())
    print(f"Connecting to {cfg.host} ...")
    if not await pad.wait_connected(30):
        print("Pad unreachable.")
        task.cancel()
        return 1
    print("Chase: one green LED walks across all keys (check the order!)")
    for i in range(NUM_KEY_LEDS):
        colors = [0] * (NUM_KEY_LEDS * 3)
        colors[i * 3 + 1] = 255
        await pad.show(colors, [f"Chase key {i + 1}"])
        await asyncio.sleep(0.3)
    for name, rgb in [("green", (0, 255, 0)), ("yellow", (255, 160, 0)),
                      ("red", (255, 0, 0)), ("off", (0, 0, 0))]:
        print(f"All keys: {name}")
        await pad.show(list(rgb) * NUM_KEY_LEDS, [f"Test: {name}"])
        await asyncio.sleep(1.0)
    task.cancel()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-monitor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daemon", help="run the daemon (via systemd)")
    sub.add_parser("hook", help="Claude Code hook entry point (reads stdin)")
    status = sub.add_parser("status", help="show session status")
    status.add_argument("--watch", action="store_true", help="refresh live")
    sub.add_parser("test-pattern", help="play an LED test pattern on the pad")
    args = parser.parse_args(argv)

    if args.cmd == "daemon":
        return _run_daemon()
    if args.cmd == "hook":
        from . import hook
        return hook.main()
    if args.cmd == "status":
        from .statusview import run_status
        return run_status(args.watch)
    if args.cmd == "test-pattern":
        return asyncio.run(_test_pattern_async())
    return 2
```

- [ ] **Step 5: Tests + manual smoke test**

Run: `uv run pytest -q`
Expected: all tests passed

Run: `uv run agent-monitor status`
Expected: output contains `daemon is not running` and `No active sessions.`

- [ ] **Step 6: Commit**

```bash
git add src/agent_monitor/statusview.py src/agent_monitor/cli.py tests/test_statusview.py
git commit -m "feat: CLI — status/--watch, daemon, hook, test-pattern"
```

---

### Task 10: ESPHome firmware (`firmware/deepdeck.yaml`)

**Files:**
- Create: `firmware/deepdeck.yaml`, `firmware/secrets.yaml.example`

- [ ] **Step 1: Write secrets.yaml.example**

`firmware/secrets.yaml.example`:
```yaml
# Copy to firmware/secrets.yaml and fill in (secrets.yaml is gitignored).
wifi_ssid: "MyWifi"
wifi_password: "secret"
# Generate with: openssl rand -base64 32  — the same key goes into
# ~/.config/agent-monitor/config.toml as api_key.
api_key: "44dXhlcmVpbkJhc2U2NEtleUhpZXJFaW5zZXR6ZW4hIQ=="
ota_password: "secret-ota"
```

- [ ] **Step 2: Write deepdeck.yaml**

`firmware/deepdeck.yaml`:
```yaml
# DeepDeck as a dumb status display for agent-monitor.
# Pins from the stock firmware (DeepSea-Developments/DeepDeck.Ahuyama.fw):
#   key LEDs: 16x WS2812/SK6812 on GPIO17 (notification LEDs GPIO23 unused)
#   OLED SSD1306 128x64: I2C SDA=GPIO21 SCL=GPIO22
esphome:
  name: deepdeck
  friendly_name: DeepDeck
  on_boot:
    then:
      - light.turn_on:
          id: key_leds
          brightness: 100%
          effect: session_status

esp32:
  board: esp32dev
  framework:
    type: esp-idf

logger:

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
  power_save_mode: none

api:
  reboot_timeout: 0s
  encryption:
    key: !secret api_key
  services:
    - service: set_state
      variables:
        colors: int[]
        lines: string[]
      then:
        - lambda: |-
            id(led_colors).assign(colors.begin(), colors.end());
            id(oled_lines).assign(lines.begin(), lines.end());
        - light.turn_on:
            id: key_leds
            brightness: 100%
            effect: session_status

ota:
  - platform: esphome
    password: !secret ota_password

globals:
  - id: led_colors
    type: std::vector<int>
    restore_value: no
    initial_value: 'std::vector<int>(48, 0)'
  - id: oled_lines
    type: std::vector<std::string>
    restore_value: no
    initial_value: 'std::vector<std::string>{}'

light:
  - platform: esp32_rmt_led_strip
    id: key_leds
    internal: true
    pin: GPIO17
    num_leds: 16
    chipset: SK6812
    rgb_order: GRB
    default_transition_length: 0s
    effects:
      - addressable_lambda:
          name: session_status
          update_interval: 100ms
          lambda: |-
            for (int i = 0; i < it.size(); i++) {
              int base = i * 3;
              if (base + 2 < (int) id(led_colors).size()) {
                it[i] = Color(id(led_colors)[base],
                              id(led_colors)[base + 1],
                              id(led_colors)[base + 2]);
              } else {
                it[i] = Color::BLACK;
              }
            }

i2c:
  sda: GPIO21
  scl: GPIO22

font:
  - file: "gfonts://Roboto Mono"
    id: font8
    size: 8

display:
  - platform: ssd1306_i2c
    model: "SSD1306 128x64"
    address: 0x3C
    update_interval: 500ms
    lambda: |-
      for (size_t i = 0; i < id(oled_lines).size() && i < 8; i++) {
        it.print(0, (int) (i * 8), id(font8), id(oled_lines)[i].c_str());
      }
```

- [ ] **Step 3: Validate the configuration**

```bash
cp firmware/secrets.yaml.example firmware/secrets.yaml
uvx esphome config firmware/deepdeck.yaml
```
Expected: rendered configuration is printed, exit code 0, no `Failed config` blocks.
If errors about `esp32_rmt_led_strip` options come up (ESPHome versions occasionally change keys like `rmt_symbols`): read the error message and adjust the offending key as instructed — pins and LED count stay as above.

- [ ] **Step 4: Commit**

```bash
git add firmware/deepdeck.yaml firmware/secrets.yaml.example
git commit -m "feat: ESPHome firmware for DeepDeck (set_state service, LEDs, OLED)"
```

---

### Task 11: Installation — systemd, hooks, README

**Files:**
- Create: `systemd/agent-monitor.service`, `README.md`, `scripts/install-hooks.py`

- [ ] **Step 1: Write systemd unit**

`systemd/agent-monitor.service`:
```ini
[Unit]
Description=Agent Monitor — Claude session status on DeepDeck
After=network-online.target

[Service]
ExecStart=%h/.local/bin/agent-monitor daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Write hook installation script**

`scripts/install-hooks.py`:
```python
#!/usr/bin/env python3
"""Registers agent-monitor hooks in ~/.claude/settings.json (with backup)."""
import json
import shutil
from pathlib import Path

EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"]
SETTINGS = Path.home() / ".claude" / "settings.json"
COMMAND = str(Path.home() / ".local" / "bin" / "agent-monitor") + " hook"


def main() -> None:
    cfg = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    if SETTINGS.exists():
        shutil.copy(SETTINGS, SETTINGS.with_suffix(".json.bak"))
    hooks = cfg.setdefault("hooks", {})
    for event in EVENTS:
        entries = hooks.setdefault(event, [])
        already = any(
            h.get("command", "").endswith("agent-monitor hook")
            for e in entries
            for h in e.get("hooks", [])
        )
        if not already:
            entries.append({"hooks": [{"type": "command", "command": COMMAND, "timeout": 5}]})
    SETTINGS.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"Hooks registered in {SETTINGS} (backup: {SETTINGS}.bak)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write README**

`README.md`:
```markdown
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

## Usage

    agent-monitor status           # table
    agent-monitor status --watch   # live
    agent-monitor test-pattern     # LED test on the pad
```

- [ ] **Step 4: Install and verify**

```bash
uv tool install --editable .
python3 scripts/install-hooks.py
mkdir -p ~/.config/systemd/user
cp systemd/agent-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-monitor
systemctl --user status agent-monitor --no-pager
```
Expected: unit `active (running)`; log line "no pad configured" (config.toml does not exist yet).

Run: `cat ~/.claude/settings.json` — Expected: `hooks` block with all 5 events, existing keys (`model`, `enabledPlugins`, …) unchanged.

- [ ] **Step 5: Verify end-to-end without hardware**

Start a **new** Claude Code session in any project folder, ask something, then:

Run: `agent-monitor status`
Expected: session appears with its project name; status switches between `working` (while Claude responds), `available` (afterwards) and `waiting for input` (on a permission prompt, e.g. from a command that needs approval).

**Verify notification texts** (open point from the spec): during the test session confirm that a permission prompt → red, and a session sitting idle stays green. If not: log the actual `message` texts (`journalctl --user -u agent-monitor`) and adjust `IDLE_MARKERS` in `model.py`, updating the tests accordingly.

- [ ] **Step 6: Commit**

```bash
git add systemd/agent-monitor.service scripts/install-hooks.py README.md
git commit -m "feat: installation — systemd unit, hook registration, README"
```

---

### Task 13: Key-press session name overlay (added on Aaron's request)

**Files:**
- Modify: `src/agent_monitor/render.py` (+`key_names()`), `src/agent_monitor/pad.py` (3-arg `show`, `SERVICE_ARGS` gains `names`), `src/agent_monitor/daemon.py` (`_refresh` passes names), `src/agent_monitor/cli.py` (test-pattern passes names), `firmware/deepdeck.yaml` (matrix keypad, overlay globals, display overlay, `set_state` gains `names: string[]`), and the corresponding tests.

Summary (full code provided at dispatch time): `render.key_names(sessions)` returns 16 full project names (≤25 chars, `""` = free key). `DeepDeckPad.show(colors, lines, names)` buffers and pushes all three; the firmware stores `names` in a global, scans the 4×4 matrix (rows GPIO 0/4/5/12, cols GPIO 16/15/14/13), and a key press sets `overlay_slot`/`overlay_until` (3 s) and forces a display update; the display lambda renders `key N` + name (or `(free)`) while the overlay is active. Task 12 additionally verifies the key→slot mapping via the overlay.

---

### Task 14: Process-scan discovery, UNKNOWN status, claude-ancestor PID (added on Aaron's request)

**Files:**
- Modify: `src/agent_monitor/model.py` (Status.UNKNOWN), `src/agent_monitor/render.py` (blue color, `?` char), `src/agent_monitor/statusview.py` (label), `src/agent_monitor/state.py` (`add_scanned`, PID-takeover on real event), `src/agent_monitor/scan.py` (new: /proc scanner), `src/agent_monitor/daemon.py` (scan loop), `src/agent_monitor/hook.py` (claude-ancestor PID walk), plus tests.

Summary (full code at dispatch): `scan.claude_pids()` finds top-most `claude`-cmdline processes in /proc with their cwds; the daemon merges them every ~20 s as `proc-<pid>` UNKNOWN sessions (skipping PIDs already tracked); `apply_event` transfers the slot from a `proc-<pid>` session to a real session with the same PID; `hook._claude_pid()` walks up from getppid() to the nearest ancestor whose cmdline contains "claude" (fallback: getppid()). Firmware is unaffected (colors arrive pre-rendered).

---

### Task 15: Claude-orange flashing (added on Aaron's request)

`set_state` gains `flash: int[]` (16 flags; daemon sets 1 for UNKNOWN slots via `render.flash_flags`). The key-LED effect applies a 1.2 s triangle pulse (15–100 %) to flagged LEDs; UNKNOWN color becomes Claude orange (217, 119, 87). The two notification LEDs (own strip, GPIO23) breathe Claude orange continuously (`claude_pulse` effect, firmware-only). CLI unknown label switches to dark_orange. Deployment note: reflash (OTA) and daemon restart must happen back-to-back — the `set_state` arity changed, so old/new combinations freeze the pad display until both sides match.

---

### Task 12: Hardware bring-up (manual, with Aaron)

**Files:**
- Modify: possibly `src/agent_monitor/render.py` (KEY_LEDS remap), `tests/test_render.py`

This task needs the physical DeepDeck over USB. Walk through the steps together with Aaron.

> **Bench notes (from firmware review):**
> 1. **If the board appears dead after plugging in** (no serial output at all): unplug, make sure no key is held down, and re-plug before suspecting the flash. GPIO12 (matrix, MTDI strapping pin) held high at power-on sets the flash voltage wrong and prevents boot entirely — a held key on its row/column can cause this. Property of the DeepDeck PCB, not the firmware.
> 2. **Verify the key matrix before the daemon is ever involved:** the overlay works standalone (no WiFi/daemon needed) — press all 16 keys and confirm the OLED shows the matching `key N` / `(free)`. If the mapping is transposed or rotated, reorder ONLY the `keys:` string in `firmware/deepdeck.yaml` and reflash (OTA) — do not edit the 16 per-key values (they no longer exist).
> 3. LED order is verified separately via `agent-monitor test-pattern` (chase); remap `KEY_LEDS` in `render.py` if needed.
> 4. If only one notification LED lights or its color is wrong, the notif strip is RGBW — set `is_rgbw: true` on `notif_leds` (same open question as the key strip).

- [ ] **Step 1: Fill in secrets**

`firmware/secrets.yaml`: enter real WiFi credentials; generate `api_key` with `openssl rand -base64 32`.

- [ ] **Step 2: Flash (USB)**

Connect the DeepDeck via USB-C, then:
```bash
uvx esphome run firmware/deepdeck.yaml
```
At the port prompt pick the CP2102 port (`/dev/ttyUSB0`). On `Permission denied`: `sudo usermod -aG dialout $USER` and log in again.
Expected: build + flash succeed, log shows `WiFi Connected` and the IP.

- [ ] **Step 3: Create PC configuration**

`~/.config/agent-monitor/config.toml`:
```toml
[pad]
enabled = true
host = "deepdeck.local"   # if mDNS does not resolve: IP from the flash log
api_key = "<value from firmware/secrets.yaml>"
```
Then: `systemctl --user restart agent-monitor`

- [ ] **Step 4: Check the test pattern**

Run: `agent-monitor test-pattern`
Expected: one green LED walks across keys 1–16 in order (top left → bottom right, row by row), then all keys light green → yellow → red → off; the OLED shows the test texts.

**If the chase order is not row-by-row** (e.g. serpentine wiring): note the observed physical order and remap the `KEY_LEDS` list in `render.py` so `KEY_LEDS[slot]` hits the physically correct LED; adjust `test_waiting_session_lights_red_on_its_slot` in `tests/test_render.py` accordingly; commit:
```bash
git add src/agent_monitor/render.py tests/test_render.py
git commit -m "fix: remap KEY_LEDS to physical LED order"
```

- [ ] **Step 5: End-to-end with real sessions**

Start several Claude sessions in parallel (different projects). Check:
- Each session gets its own key; the OLED lists `no project statuschar`.
- A working session = yellow, a finished one = green, a permission prompt = red.
- Ending a session → LED goes off, key becomes free again.
- Briefly cut the pad's WiFi (plug/router) → after reconnect the pad shows the correct current state again.

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "chore: hardware bring-up complete"
```

---

### Task 20: Context %, model, effort on the overlay; account usage as idle screen (added on Aaron's request, 2026-08-09)

New module `context.py` reads Claude Code's own files (read-only): `~/.claude/sessions/<pid>.json` maps the tracked PID to sessionId/cwd, the transcript tail's last main-thread assistant entry yields context tokens (`input + cache_read + cache_creation`), model and `effort`, and `~/.claude.json → cachedUsageUtilization` yields the 5h/7d account limits `/usage` shows. `Session` gains `context_pct/model/effort`; the daemon polls both via a 10 s `_ctx_loop`. `set_state` gains `info: string[]` ('\n'-separated overlay lines) and `usage: int[]` (bar fills, −1 = stale/no bar); the previously idle-unused `lines` now carries the usage text. Firmware: overlay prints model+effort and `ctx N%` under the project name; the idle screen renders "Claude usage" with bars, kraken as no-data fallback. CLI status gains Model and Ctx columns.

> **Finding (window inference):** the 1m context beta rides an HTTP header — transcripts record a plain model string even at 700k+ tokens. The window is therefore inferred: `[1m]` tag, any observed turn >200k tokens, or the session running the settings-pinned 1m default model ⇒ 1m; else 200k (conservative, self-corrects once evidence appears).
> **Finding (usage staleness):** `cachedUsageUtilization` refreshes on Claude Code's own schedule (~40 min gaps observed); once a limit's reset time passes, the cached percentage is knowably wrong → shown as `--%` with no bar until the cache refreshes.

---

### Task 21: "compact context" pad-menu action (added on Aaron's request, 2026-08-09)

Third context-menu entry (option 2). The phone app has no manual compact; the deck now does: `actions.compact_session` types `/compact` via the shared `_slash_command` helper (extracted from `toggle_remote_control`). Firmware menu cycles mod 3, anticlockwise steps back; daemon maps options via a dict and ignores unknown ones.

> **AMENDMENT (Task 20, same day):** `cachedUsageUtilization` refreshes only on LOCAL UI activity — a phone-driven remote-control session never touches it (observed: cache frozen for 3 h while the phone showed fresh numbers). `UsageProvider` therefore prefers the cache while it is <10 min old and non-stale, and otherwise fetches `api.anthropic.com/api/oauth/usage` directly with the on-disk Claude Code OAuth token (read-only, ≥10 min between fetches, 2 min failure backoff, token never logged). Verified live: deck shows the post-reset 5h window while the PC UI is idle.

> **AMENDMENT (Task 21, same day):** pad actions on a LOCKED X session silently type into the screen locker — Aaron's first real-world compact attempt (locked PC, away from home) reported "ok" while nothing reached VS Code (transcripts unchanged). All actions now check `loginctl` LockedHint for the session owning $DISPLAY first and refuse with a clear log line. Boundary documented in README; the deck's "sent" toast stays (no result channel to the pad).

---

### Task 22: Feedback, warnings, right knob (added on Aaron's request, 2026-08-09)

Bundle of four: (1) truthful action feedback — the daemon pre-checks `actions.display_locked` and pushes the real result (`reloading`/`remote on|off`/`compacting`/`locked`/`failed`) as an overlay note; the firmware reopens the key overlay after a menu confirm instead of the blind "sent" toast. (2) `agent-monitor status` shows the account usage line (state.json carries `usage`). (3) High-context warning: `!` behind ctx ≥ 85 % on OLED + CLI (red bold); the two notification LEDs (GPIO23) become a STATIC usage warning — amber ≥ 75 %, red ≥ 90 % of the 5h limit, dark otherwise (decorative glow stays reverted). (4) Right knob (encoder 2: A=GPIO33 B=GPIO32, switch GPIO27 per stock keyboard_config.h): rotation browses occupied keys' overlays, button = one-press compact of the shown session.

---

### Task 23: Cross-session waits and usage-limit blocks (added on Aaron's request, 2026-08-10)

Two invisible states, both found on the bench: (1) a session that dispatched work to a PEER session via `SendMessage` sits idle with a Stop as its last event and read as green ("finished, come look") while the answer was being written elsewhere. `read_context` now records the addressed peer (name form `"peer-9e [ref]"` and reply form `"uds:/run/user/1000/cc-socks/<pid>.sock"`, resolved via `~/.claude/sessions/*.json`), and the registry sets `waiting_for` while that peer is busy/waiting/blocked → yellow, never green, with `waits: <peer>` on the OLED overlay. Window: 30 min, self-healing. (2) A turn that ends on a usage-limit notice ("You've reached your Fable 5 limit. Run /usage-credits…") leaves the session unable to continue without the user, and no hook reports it: detected as a lone short text block whose limit phrase OPENS the message (prose *about* limits must never light a key red) → `blocked` → red, cleared automatically when the conversation continues.

> **AMENDMENT (Task 23):** Esc-interrupts end a turn WITHOUT a Stop hook, so the key stayed yellow forever (and the activity detector kept re-arming it, because the interrupt itself writes the transcript). `read_context` now reports `interrupted` when the last main-chain entry is exactly Claude Code's marker (`[Request interrupted by user]` / `[... for tool use]`, both observed in Aaron's transcripts; prose quoting the marker must not match), and the registry claims it BEFORE the activity check → AVAILABLE + finished (green), also beating a pending peer wait. The next `UserPromptSubmit` returns it to working.

---

### Task 24: Remote sessions over SSH (added on Aaron's request, 2026-08-11)

A VS Code Remote-SSH window runs Claude Code entirely on the remote host: process, hooks and transcript are all there, so nothing local could see it. New `remote.py` pipes a self-contained probe — this package's `context.py` plus a `__main__` block, stdlib-only and 3.10-compatible — through `ssh <host> python3 -` and reads JSON back; **nothing is installed on the remote host**. Hosts are auto-discovered from the running Remote-SSH tunnel processes (`ssh -v -T -D <port> … <host>`), so the daemon only ever dials a machine the user is already connected to; `BatchMode=yes` never prompts and `ControlPersist` reuses the connection between 30 s probes.

Remote sessions have **no hooks**, so their whole status comes from the transcript facts this project already derives (activity age → working, interrupt marker → green, limit notice / pending question → red). The probe reports an *age*, not a timestamp, so the two machines' clocks need not agree. `Session.host` marks them: `prune()` skips them (their pid is not ours to check) and only a *successful* probe removes them — an unreachable host keeps its keys instead of blinking out. Sticky-key memory is keyed `host:cwd`, so the same project name on two machines gets two keys. Deck overlay and CLI show `@<host>`.

> **AMENDMENT (Task 24):** remote-control state must be read where the sockets are, so the probe now carries `scan.py` too — `probe_script()` concatenates `context.py` + `scan.py` + the main, stripping the duplicate `from __future__` lines (only legal as a file's first statement) and re-adding one at the top. `sync_remote` adopts the reported flag unless a pad toggle pinned it (`rc_manual`), exactly as for local sessions. Verified live: the spheron-AI-PC session reports `remote: true` and its key turns blue.

> **AMENDMENT (Task 21/24):** after a reboot every desktop action silently failed with `no window`. Cause: the user service is enabled at boot and starts BEFORE the graphical session publishes DISPLAY into the systemd user environment, so the daemon ran with no DISPLAY at all — `wmctrl -l` printed nothing and the matcher had no candidates (the window list was fine; the rewrite in 6cbf487 was not at fault). Fix: `focus.ensure_display()` recovers DISPLAY/XAUTHORITY at call time (systemd user environment → X socket in /tmp/.X11-unix → gdm/home Xauthority) so actions survive a boot-order race AND an X restart without a daemon restart; both `focus_window` and the palette injection call it, and the unit now also orders itself after `graphical-session.target`.

> **CORRECTION (DISPLAY fix):** the first unit-file attempt was ineffective and partly wrong — an `[Install]` change needs a re-enable to create the symlink (it never did, so the boot order was unchanged), and `Wants=graphical-session.target` is the wrong mechanism for a passive `RefuseManualStart=yes` target. The unit now only *orders* after it (`After=`, no `Wants=`) and stays `WantedBy=default.target` so a headless boot still runs the deck. The actual fix is the runtime `ensure_display()` recovery, verified from an environment with no DISPLAY/XAUTHORITY at all.
