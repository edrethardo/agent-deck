"""Focus the desktop window belonging to a session (X11, wmctrl)."""

from __future__ import annotations

import logging
import subprocess

from .render import project_name

_LOGGER = logging.getLogger(__name__)


def focus_window(cwd: str, *, run=subprocess.run) -> bool:
    """Raise the first window whose title contains the session's project name."""
    name = project_name(cwd).lower()
    if not name:
        return False
    try:
        out = run(["wmctrl", "-l"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.warning("wmctrl unavailable: %s", exc)
        return False
    candidates = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and name in parts[3].lower():
            candidates.append((0 if "visual studio code" in parts[3].lower() else 1, parts[0]))
    if not candidates:
        _LOGGER.info("no window matching %r", name)
        return False
    candidates.sort()
    try:
        run(["wmctrl", "-i", "-a", candidates[0][1]], timeout=2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.warning("wmctrl focus failed: %s", exc)
        return False
    return True
