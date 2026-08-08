from __future__ import annotations

import os
from pathlib import Path


def _runtime_dir() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/agent-monitor-{os.getuid()}"))
    d = base / "agent-monitor"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def socket_path() -> Path:
    return _runtime_dir() / "daemon.sock"


def state_path() -> Path:
    return _runtime_dir() / "state.json"


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "agent-monitor" / "config.toml"
