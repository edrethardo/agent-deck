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
    """None if the file is missing or invalid, [pad] is missing or disabled,
    or host is not set — the daemon then runs without a pad."""
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    pad = data.get("pad", {})
    if not pad.get("enabled", False) or not pad.get("host"):
        return None
    try:
        return PadConfig(
            host=str(pad["host"]),
            api_key=str(pad.get("api_key", "")),
            port=int(pad.get("port", 6053)),
            reconnect_delay=float(pad.get("reconnect_delay", 5.0)),
        )
    except (ValueError, TypeError):
        return None
