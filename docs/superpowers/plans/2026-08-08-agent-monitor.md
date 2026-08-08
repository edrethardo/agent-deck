# Agent Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ampel-Anzeige (grün/gelb/rot) für alle lokalen Claude-Code-Sessions auf einem DeepDeck-Macropad (ESPHome) plus Terminal-CLI.

**Architecture:** Claude-Code-Hooks schicken Events an einen lokalen Daemon (Unix-Socket). Der Daemon hält eine Session-Registry (Status, Slot 0–15, PID), rendert daraus LED-Farben + OLED-Zeilen und pusht sie per ESPHome native API ans Pad; parallel schreibt er `state.json` für die CLI. Das Pad ist eine dumme Anzeige (ESPHome-Firmware mit einem `set_state`-Service).

**Tech Stack:** Python 3.12, `uv`, `aioesphomeapi`, `rich`, pytest + pytest-asyncio (`asyncio_mode=auto`), ESPHome (via `uvx`), systemd user service.

**Spec:** `docs/superpowers/specs/2026-08-08-agent-monitor-design.md`

**Hardware-Fakten** (aus der Stock-Firmware `DeepSea-Developments/DeepDeck.Ahuyama.fw` extrahiert):
- Tasten-LEDs: **16× WS2812/SK6812 an GPIO17** (eigener Strip; Slot i = LED i, ggf. Remap nach Hardware-Test)
- Notification-LEDs: 2× an GPIO23 — **in v1 ungenutzt**
- OLED: SSD1306 128×64, I2C **SDA=GPIO21, SCL=GPIO22**, Adresse 0x3C
- Board: ESP32-WROOM-32D (`esp32dev`), USB-Serial CP2102N

**Datenformate (überall identisch):**
- Hook→Daemon (JSON-Zeile über Unix-Socket): `{"event": "Stop", "session_id": "...", "cwd": "/pfad", "pid": 12345, "message": null}`
- `state.json`: `{"updated": 1723.0, "sessions": [{"session_id": "...", "cwd": "...", "pid": 1, "status": "available", "slot": 0, "since": 1723.0}]}`
- Daemon→Pad (ESPHome-Service `set_state`): `colors` = 48 ints (16 LEDs × RGB, 0–255), `lines` = bis zu 8 Strings.

**Dateistruktur:**

```
pyproject.toml
.gitignore
src/agent_monitor/
  __init__.py    (leer)
  model.py       Status-Enum, Session-Dataclass, status_for_event()
  state.py       SessionRegistry (Slots, apply_event, prune, (De-)Serialisierung)
  render.py      Sessions → LED-Farben + OLED-Zeilen
  paths.py       Socket-/State-/Config-Pfade (XDG)
  config.py      PadConfig aus config.toml
  hook.py        Hook-Client (stdin-JSON → Socket, niemals Fehler)
  pad.py         DeepDeckPad (aioesphomeapi, Reconnect, Full-State-Push)
  daemon.py      Daemon (Socket-Server, Refresh, Prune-Loop)
  statusview.py  CLI-Tabelle (+ --watch)
  cli.py         argparse-Einstieg: daemon | hook | status | test-pattern
tests/
  test_model.py test_state.py test_render.py test_config.py
  test_hook.py test_pad.py test_daemon.py test_statusview.py
firmware/
  deepdeck.yaml
  secrets.yaml.example
systemd/agent-monitor.service
```

---

### Task 1: Projektgerüst

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/agent_monitor/__init__.py`, `tests/test_smoke.py`

- [ ] **Step 1: pyproject.toml schreiben**

```toml
[project]
name = "agent-monitor"
version = "0.1.0"
description = "Ampel-Status für Claude-Code-Sessions auf DeepDeck + CLI"
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

- [ ] **Step 2: .gitignore schreiben**

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

- [ ] **Step 3: Paket + Smoke-Test anlegen**

`src/agent_monitor/__init__.py`: leere Datei.

`tests/test_smoke.py`:
```python
def test_import():
    import agent_monitor  # noqa: F401
```

- [ ] **Step 4: Sync + Tests laufen lassen**

Run: `uv sync && uv run pytest -q`
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore src tests uv.lock
git commit -m "chore: Projektgerüst (uv, pytest, Paket agent_monitor)"
```

---

### Task 2: Statuslogik (`model.py`)

**Files:**
- Create: `src/agent_monitor/model.py`
- Test: `tests/test_model.py`

- [ ] **Step 1: Failing Tests schreiben**

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

- [ ] **Step 2: Tests laufen lassen — müssen fehlschlagen**

Run: `uv run pytest tests/test_model.py -q`
Expected: FAIL / ERROR mit `ModuleNotFoundError: No module named 'agent_monitor.model'`

- [ ] **Step 3: Implementierung**

`src/agent_monitor/model.py`:
```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    AVAILABLE = "available"  # grün
    BUSY = "busy"            # gelb
    WAITING = "waiting"      # rot


@dataclass
class Session:
    session_id: str
    cwd: str
    pid: int
    status: Status
    slot: int | None  # 0-15, None = Overflow (keine LED)
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


# Notification-Texte, die KEINE Blockierung bedeuten (Session steht nur untätig
# am Prompt) — laut Spec bleibt der Status dann unverändert.
IDLE_MARKERS = ("waiting for your input",)


def status_for_event(event: str, message: str | None, current: Status | None) -> Status | None:
    """Neuer Status für ein Hook-Event; None = Status nicht ändern."""
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

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_model.py -q`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/model.py tests/test_model.py
git commit -m "feat: Statuslogik — Hook-Events auf Ampelstatus abbilden"
```

---

### Task 3: SessionRegistry (`state.py`)

**Files:**
- Create: `src/agent_monitor/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Failing Tests schreiben**

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

- [ ] **Step 2: Tests laufen lassen — müssen fehlschlagen**

Run: `uv run pytest tests/test_state.py -q`
Expected: FAIL mit `ModuleNotFoundError: No module named 'agent_monitor.state'`

- [ ] **Step 3: Implementierung**

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
        """Sortiert nach Slot, Overflow (None) zuletzt."""
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
        """Event einarbeiten. True, wenn sich der Anzeigezustand geändert hat."""
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
        """Sessions toter Prozesse entfernen. True bei Änderung."""
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

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_state.py -q`
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/state.py tests/test_state.py
git commit -m "feat: SessionRegistry mit Slot-Vergabe, Overflow und Prune"
```

---

### Task 4: Rendering (`render.py`)

**Files:**
- Create: `src/agent_monitor/render.py`
- Test: `tests/test_render.py`

- [ ] **Step 1: Failing Tests schreiben**

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

- [ ] **Step 2: Tests laufen lassen — müssen fehlschlagen**

Run: `uv run pytest tests/test_render.py -q`
Expected: FAIL mit `ModuleNotFoundError: No module named 'agent_monitor.render'`

- [ ] **Step 3: Implementierung**

`src/agent_monitor/render.py`:
```python
from __future__ import annotations

import os

from .model import Session, Status

NUM_KEY_LEDS = 16
# Physische LED-Reihenfolge auf dem Strip. Falls der Hardware-Test (Task 12)
# eine andere Verdrahtung zeigt (z.B. Serpentinen), hier remappen.
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

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_render.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/render.py tests/test_render.py
git commit -m "feat: Rendering von Sessions zu LED-Farben und OLED-Zeilen"
```

---

### Task 5: Pfade und Konfiguration (`paths.py`, `config.py`)

**Files:**
- Create: `src/agent_monitor/paths.py`, `src/agent_monitor/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Failing Tests schreiben**

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

- [ ] **Step 2: Tests laufen lassen — müssen fehlschlagen**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL mit `ModuleNotFoundError`

- [ ] **Step 3: Implementierung**

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
    """None, wenn Datei fehlt, [pad] fehlt oder enabled=false — Daemon läuft dann ohne Pad."""
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

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_config.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/paths.py src/agent_monitor/config.py tests/test_config.py
git commit -m "feat: XDG-Pfade und Pad-Konfiguration aus config.toml"
```

---

### Task 6: Hook-Client (`hook.py`)

**Files:**
- Create: `src/agent_monitor/hook.py`
- Test: `tests/test_hook.py`

- [ ] **Step 1: Failing Tests schreiben**

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
    _fake_stdin(monkeypatch, "kein json")
    assert hook.main() == 0
```

- [ ] **Step 2: Tests laufen lassen — müssen fehlschlagen**

Run: `uv run pytest tests/test_hook.py -q`
Expected: FAIL mit `ModuleNotFoundError: No module named 'agent_monitor.hook'`

- [ ] **Step 3: Implementierung**

`src/agent_monitor/hook.py`:
```python
"""Hook-Client: wird von Claude Code aufgerufen. Darf NIEMALS fehlschlagen
oder blockieren — jeder Fehler wird geschluckt, Exit-Code ist immer 0."""

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
            "pid": os.getppid(),  # Parent = der Claude-Prozess
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

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_hook.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/hook.py tests/test_hook.py
git commit -m "feat: Hook-Client — stdin-JSON an Daemon-Socket, fehlertolerant"
```

---

### Task 7: Pad-Client (`pad.py`)

**Files:**
- Create: `src/agent_monitor/pad.py`
- Test: `tests/test_pad.py`

**Hintergrund für den Implementierer:** `aioesphomeapi.APIClient` spricht die ESPHome native API. Relevante Methoden: `await client.connect(login=True, on_stop=cb)` (cb ist ein async Callable `(expected_disconnect: bool) -> None`, wird bei Verbindungsabbruch gerufen), `await client.list_entities_services()` → `(entities, services)`, `client.execute_service(service, {...})` (synchron, fire-and-forget), `await client.disconnect()`. Wir injizieren eine `client_factory`, damit Tests ohne Netzwerk laufen.

- [ ] **Step 1: Failing Tests schreiben**

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
    await pad.show([1, 2, 3], ["zeile"])
    assert created[0].calls[-1] == {"colors": [1, 2, 3], "lines": ["zeile"]}
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
    await created[0].on_stop(False)  # Verbindungsabbruch simulieren
    await asyncio.sleep(0.1)
    assert len(created) >= 2
    assert created[1].calls == [{"colors": [5], "lines": ["x"]}]
    task.cancel()


async def test_show_without_connection_does_not_raise(fakes):
    _, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    await pad.show([1], [])  # kein run() gestartet — darf nicht werfen
```

- [ ] **Step 2: Tests laufen lassen — müssen fehlschlagen**

Run: `uv run pytest tests/test_pad.py -q`
Expected: FAIL mit `ModuleNotFoundError: No module named 'agent_monitor.pad'`

- [ ] **Step 3: Implementierung**

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
    """Hält die Verbindung zum DeepDeck und pusht den Anzeigezustand.

    `show()` merkt sich immer den letzten Zustand; nach jedem (Re-)Connect
    wird der komplette Zustand erneut gepusht — nie nur Deltas.
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
                    raise RuntimeError(f"Service {SERVICE_NAME!r} fehlt auf dem Pad")
                self._client = client
                self._connected.set()
                await self._push_last()
                await stopped.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("Pad-Verbindung fehlgeschlagen: %s", exc)
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
            _LOGGER.warning("Pad-Push fehlgeschlagen: %s", exc)
```

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_pad.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/agent_monitor/pad.py tests/test_pad.py
git commit -m "feat: DeepDeckPad — ESPHome-Client mit Reconnect und Full-State-Push"
```

---

### Task 8: Daemon (`daemon.py`)

**Files:**
- Create: `src/agent_monitor/daemon.py`
- Test: `tests/test_daemon.py`

- [ ] **Step 1: Failing Tests schreiben**

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
    assert colors[1] > 0  # Slot 0 leuchtet grün
    assert lines == [" 1 x            +"]
    task.cancel()


async def test_invalid_lines_are_ignored(paths):
    state_path, sock_path = paths
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await daemon.ready.wait()
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write(b"kein json\n")
    writer.write(json.dumps({"event": "Stop"}).encode() + b"\n")  # ohne session_id
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

- [ ] **Step 2: Tests laufen lassen — müssen fehlschlagen**

Run: `uv run pytest tests/test_daemon.py -q`
Expected: FAIL mit `ModuleNotFoundError: No module named 'agent_monitor.daemon'`

- [ ] **Step 3: Implementierung**

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
        # Kompletten Snapshot inkl. Slots übernehmen — so behalten Sessions
        # ihre Taste auch über einen Daemon-Neustart hinweg.
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

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_daemon.py -q`
Expected: `5 passed`

- [ ] **Step 5: Alle Tests laufen lassen**

Run: `uv run pytest -q`
Expected: `42 passed` (Smoke 1 + Model 7 + State 11 + Render 6 + Config 4 + Hook 4 + Pad 4 + Daemon 5), 0 failed

- [ ] **Step 6: Commit**

```bash
git add src/agent_monitor/daemon.py tests/test_daemon.py
git commit -m "feat: Daemon — Socket-Server, State-Datei, Pad-Push, Prune-Loop"
```

---

### Task 9: CLI (`statusview.py`, `cli.py`)

**Files:**
- Create: `src/agent_monitor/statusview.py`, `src/agent_monitor/cli.py`
- Test: `tests/test_statusview.py`

- [ ] **Step 1: Failing Tests schreiben**

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
    assert "wartet auf Input" in out
    assert "arbeitet" in out
    assert "verfügbar" in out
    assert "1m0s" in out  # waiting seit 60s


def test_overflow_session_shows_dash_for_key():
    out = render_status(_state(), now=100.0, daemon_up=True)
    assert "—" in out


def test_daemon_down_warning():
    out = render_status(None, now=100.0, daemon_up=False)
    assert "Daemon läuft nicht" in out


def test_empty_state_message():
    out = render_status({"updated": 1.0, "sessions": []}, now=2.0, daemon_up=True)
    assert "Keine aktiven Sessions" in out


def test_format_duration():
    assert format_duration(5) == "5s"
    assert format_duration(65) == "1m5s"
    assert format_duration(3725) == "1h02m"
```

- [ ] **Step 2: Tests laufen lassen — müssen fehlschlagen**

Run: `uv run pytest tests/test_statusview.py -q`
Expected: FAIL mit `ModuleNotFoundError`

- [ ] **Step 3: statusview implementieren**

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
    "available": ("verfügbar", "green"),
    "busy": ("arbeitet", "yellow"),
    "waiting": ("wartet auf Input", "red bold"),
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
        console.print("[red]⚠ Daemon läuft nicht[/red] — starte ihn mit: "
                      "systemctl --user start agent-monitor")
    sessions = (state or {}).get("sessions", [])
    if not sessions:
        console.print("Keine aktiven Sessions.")
        return console.file.getvalue()

    table = Table()
    table.add_column("Taste", justify="right")
    table.add_column("Projekt")
    table.add_column("Status")
    table.add_column("Seit", justify="right")
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

- [ ] **Step 4: cli.py implementieren**

`src/agent_monitor/cli.py`:
```python
from __future__ import annotations

import argparse
import asyncio
import logging

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
        logging.info("Kein Pad konfiguriert (%s) — läuft ohne Hardware", paths.config_path())
    daemon = Daemon(SessionRegistry(), pad, paths.state_path(), paths.socket_path())
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    return 0


async def _test_pattern_async() -> int:
    from .config import load_pad_config
    from .pad import DeepDeckPad
    from .render import NUM_KEY_LEDS

    cfg = load_pad_config(paths.config_path())
    if cfg is None:
        print(f"Kein Pad konfiguriert — {paths.config_path()} anlegen (siehe README).")
        return 1
    pad = DeepDeckPad(cfg)
    task = asyncio.create_task(pad.run())
    print(f"Verbinde mit {cfg.host} ...")
    if not await pad.wait_connected(30):
        print("Pad nicht erreichbar.")
        task.cancel()
        return 1
    print("Chase: eine grüne LED wandert über alle Tasten (Reihenfolge prüfen!)")
    for i in range(NUM_KEY_LEDS):
        colors = [0] * (NUM_KEY_LEDS * 3)
        colors[i * 3 + 1] = 255
        await pad.show(colors, [f"Chase Taste {i + 1}"])
        await asyncio.sleep(0.3)
    for name, rgb in [("gruen", (0, 255, 0)), ("gelb", (255, 160, 0)),
                      ("rot", (255, 0, 0)), ("aus", (0, 0, 0))]:
        print(f"Alle Tasten: {name}")
        await pad.show(list(rgb) * NUM_KEY_LEDS, [f"Test: {name}"])
        await asyncio.sleep(1.0)
    task.cancel()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-monitor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daemon", help="Daemon starten (via systemd)")
    sub.add_parser("hook", help="Claude-Code-Hook-Einstieg (liest stdin)")
    status = sub.add_parser("status", help="Session-Status anzeigen")
    status.add_argument("--watch", action="store_true", help="live aktualisieren")
    sub.add_parser("test-pattern", help="LED-Testmuster auf dem Pad abspielen")
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

- [ ] **Step 5: Tests + manueller Smoke-Test**

Run: `uv run pytest -q`
Expected: alle Tests passed

Run: `uv run agent-monitor status`
Expected: Ausgabe enthält `Daemon läuft nicht` und `Keine aktiven Sessions.`

- [ ] **Step 6: Commit**

```bash
git add src/agent_monitor/statusview.py src/agent_monitor/cli.py tests/test_statusview.py
git commit -m "feat: CLI — status/--watch, daemon, hook, test-pattern"
```

---

### Task 10: ESPHome-Firmware (`firmware/deepdeck.yaml`)

**Files:**
- Create: `firmware/deepdeck.yaml`, `firmware/secrets.yaml.example`

- [ ] **Step 1: secrets.yaml.example schreiben**

`firmware/secrets.yaml.example`:
```yaml
# Nach firmware/secrets.yaml kopieren und ausfüllen (secrets.yaml ist gitignored).
wifi_ssid: "MeinWLAN"
wifi_password: "geheim"
# Erzeugen mit: openssl rand -base64 32  — derselbe Key kommt in
# ~/.config/agent-monitor/config.toml als api_key.
api_key: "44dXhlcmVpbkJhc2U2NEtleUhpZXJFaW5zZXR6ZW4hIQ=="
ota_password: "geheim-ota"
```

- [ ] **Step 2: deepdeck.yaml schreiben**

`firmware/deepdeck.yaml`:
```yaml
# DeepDeck als dumme Statusanzeige für agent-monitor.
# Pins aus der Stock-Firmware (DeepSea-Developments/DeepDeck.Ahuyama.fw):
#   Tasten-LEDs: 16x WS2812/SK6812 an GPIO17 (Notification-LEDs GPIO23 ungenutzt)
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

- [ ] **Step 3: Konfiguration validieren**

```bash
cp firmware/secrets.yaml.example firmware/secrets.yaml
uvx esphome config firmware/deepdeck.yaml
```
Expected: gerenderte Konfiguration wird ausgegeben, Exit-Code 0, keine `Failed config`-Blöcke.
Falls Fehler zu `esp32_rmt_led_strip`-Optionen kommen (ESPHome-Versionen ändern hier gelegentlich Keys wie `rmt_symbols`): Fehlermeldung lesen und den beanstandeten Key gemäß Meldung anpassen — Pins und LED-Anzahl bleiben wie oben.

- [ ] **Step 4: Commit**

```bash
git add firmware/deepdeck.yaml firmware/secrets.yaml.example
git commit -m "feat: ESPHome-Firmware für DeepDeck (set_state-Service, LEDs, OLED)"
```

---

### Task 11: Installation — systemd, Hooks, README

**Files:**
- Create: `systemd/agent-monitor.service`, `README.md`, `scripts/install-hooks.py`

- [ ] **Step 1: systemd-Unit schreiben**

`systemd/agent-monitor.service`:
```ini
[Unit]
Description=Agent Monitor — Claude-Session-Status auf DeepDeck
After=network-online.target

[Service]
ExecStart=%h/.local/bin/agent-monitor daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Hook-Installations-Skript schreiben**

`scripts/install-hooks.py`:
```python
#!/usr/bin/env python3
"""Registriert agent-monitor-Hooks in ~/.claude/settings.json (mit Backup)."""
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
    print(f"Hooks registriert in {SETTINGS} (Backup: {SETTINGS}.bak)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: README schreiben**

`README.md`:
```markdown
# agent-monitor

Ampel-Status für Claude-Code-Sessions: 🟢 verfügbar · 🟡 arbeitet · 🔴 wartet auf Input.
Anzeige auf einem DeepDeck-Macropad (ESPHome, 16 Tasten-LEDs + OLED) und als CLI.

## Installation (PC)

    uv tool install --editable .          # installiert ~/.local/bin/agent-monitor
    python3 scripts/install-hooks.py      # registriert Claude-Code-Hooks
    mkdir -p ~/.config/systemd/user
    cp systemd/agent-monitor.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now agent-monitor

Laufende Claude-Sessions melden sich erst nach einem Neustart der Session
(Hooks werden beim Start geladen).

## Pad-Konfiguration (~/.config/agent-monitor/config.toml)

    [pad]
    enabled = true
    host = "deepdeck.local"      # oder feste IP
    api_key = "<derselbe Key wie in firmware/secrets.yaml>"

Ohne diese Datei läuft der Daemon ohne Hardware (nur CLI).

## Firmware flashen (einmalig, per USB)

    cp firmware/secrets.yaml.example firmware/secrets.yaml   # ausfüllen!
    uvx esphome run firmware/deepdeck.yaml                   # danach: OTA über WiFi

## Benutzung

    agent-monitor status           # Tabelle
    agent-monitor status --watch   # live
    agent-monitor test-pattern     # LED-Test auf dem Pad
```

- [ ] **Step 4: Installieren und verifizieren**

```bash
uv tool install --editable .
python3 scripts/install-hooks.py
mkdir -p ~/.config/systemd/user
cp systemd/agent-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-monitor
systemctl --user status agent-monitor --no-pager
```
Expected: Unit `active (running)`; Log-Zeile „Kein Pad konfiguriert" (config.toml existiert noch nicht).

Run: `cat ~/.claude/settings.json` — Expected: `hooks`-Block mit allen 5 Events, bestehende Keys (`model`, `enabledPlugins`, …) unverändert.

- [ ] **Step 5: End-to-End ohne Hardware verifizieren**

In einem beliebigen Projektordner eine **neue** Claude-Code-Session starten, etwas fragen, dann:

Run: `agent-monitor status`
Expected: Session erscheint mit Projektnamen; Status wechselt zwischen `arbeitet` (während Claude antwortet), `verfügbar` (danach) und `wartet auf Input` (bei einem Permission-Prompt, z.B. durch einen Befehl, der eine Freigabe braucht).

**Notification-Texte verifizieren** (offener Punkt aus der Spec): Während der Testsession prüfen, dass ein Permission-Prompt → rot führt und eine untätig herumstehende Session grün bleibt. Falls nicht: tatsächliche `message`-Texte loggen (`journalctl --user -u agent-monitor`) und `IDLE_MARKERS` in `model.py` anpassen, Tests nachziehen.

- [ ] **Step 6: Commit**

```bash
git add systemd/agent-monitor.service scripts/install-hooks.py README.md
git commit -m "feat: Installation — systemd-Unit, Hook-Registrierung, README"
```

---

### Task 12: Hardware-Inbetriebnahme (manuell, mit Aaron)

**Files:**
- Modify: ggf. `src/agent_monitor/render.py` (KEY_LEDS-Remap), `tests/test_render.py`

Dieser Task braucht das physische DeepDeck per USB. Schritte einzeln mit Aaron durchgehen.

- [ ] **Step 1: Secrets ausfüllen**

`firmware/secrets.yaml`: echte WiFi-Zugangsdaten eintragen; `api_key` erzeugen mit `openssl rand -base64 32`.

- [ ] **Step 2: Flashen (USB)**

DeepDeck per USB-C anschließen, dann:
```bash
uvx esphome run firmware/deepdeck.yaml
```
Bei Port-Auswahl den CP2102-Port (`/dev/ttyUSB0`) wählen. Bei `Permission denied`: `sudo usermod -aG dialout $USER` und neu einloggen.
Expected: Build + Flash erfolgreich, Log zeigt `WiFi Connected` und die IP.

- [ ] **Step 3: PC-Konfiguration anlegen**

`~/.config/agent-monitor/config.toml`:
```toml
[pad]
enabled = true
host = "deepdeck.local"   # falls mDNS nicht auflöst: IP aus dem Flash-Log
api_key = "<Wert aus firmware/secrets.yaml>"
```
Dann: `systemctl --user restart agent-monitor`

- [ ] **Step 4: Testmuster prüfen**

Run: `agent-monitor test-pattern`
Expected: eine grüne LED wandert der Reihe nach über Taste 1–16 (oben links → unten rechts, zeilenweise), dann leuchten alle Tasten grün → gelb → rot → aus; das OLED zeigt die Testtexte.

**Falls die Chase-Reihenfolge nicht zeilenweise läuft** (z.B. Serpentinen-Verdrahtung): beobachtete physische Reihenfolge notieren und in `render.py` die `KEY_LEDS`-Liste so remappen, dass `KEY_LEDS[slot]` die physisch richtige LED trifft; `test_waiting_session_lights_red_on_its_slot` in `tests/test_render.py` entsprechend anpassen; committen:
```bash
git add src/agent_monitor/render.py tests/test_render.py
git commit -m "fix: KEY_LEDS an physische LED-Reihenfolge angepasst"
```

- [ ] **Step 5: End-to-End mit echten Sessions**

Mehrere Claude-Sessions parallel starten (verschiedene Projekte). Prüfen:
- Jede Session bekommt eine eigene Taste; OLED listet `Nr Projekt Statuszeichen`.
- Arbeitende Session = gelb, fertige = grün, Permission-Prompt = rot.
- Session beenden → LED geht aus, Taste wird wieder frei.
- WLAN des Pads kurz trennen (Stecker/Router) → nach Reconnect zeigt das Pad wieder den korrekten aktuellen Stand.

- [ ] **Step 6: Abschluss-Commit**

```bash
git add -A
git commit -m "chore: Hardware-Inbetriebnahme abgeschlossen"
```
