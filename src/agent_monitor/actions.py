"""Desktop actions triggered from the pad (X11: wmctrl + xdotool).

All injection is command-palette-driven: palette text either matches a
registered command or harmlessly does nothing — no free-typing into editors.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time

from .focus import focus_window

_LOGGER = logging.getLogger(__name__)


def _xdo(args: list[str], *, run=subprocess.run) -> None:
    run(["xdotool", *args], timeout=5)


def _palette(command_text: str, *, run=subprocess.run, pause=time.sleep) -> bool:
    """Open the VS Code command palette in the focused window, run a command."""
    if shutil.which("xdotool") is None:
        _LOGGER.warning("xdotool not installed — pad menu actions need it")
        return False
    try:
        _xdo(["key", "--clearmodifiers", "ctrl+shift+p"], run=run)
        pause(0.35)
        _xdo(["type", "--delay", "20", command_text], run=run)
        pause(0.25)
        _xdo(["key", "Return"], run=run)
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.warning("palette injection failed: %s", exc)
        return False


def restart_session(cwd: str, *, run=subprocess.run, pause=time.sleep) -> bool:
    """Reload the session's VS Code window (hooks load fresh on reload)."""
    if not focus_window(cwd):
        return False
    pause(0.4)
    return _palette("Developer: Reload Window", run=run, pause=pause)


def _slash_command(cwd: str, command: str, *, run=subprocess.run, pause=time.sleep) -> bool:
    """Focus the Claude input via its palette command, then send a slash command."""
    if not focus_window(cwd):
        return False
    pause(0.4)
    if not _palette("Claude Code: Focus input", run=run, pause=pause):
        return False
    pause(0.6)
    try:
        _xdo(["type", "--delay", "25", command], run=run)
        pause(0.4)
        _xdo(["key", "Return"], run=run)  # accept the slash-command autocomplete
        pause(0.2)
        _xdo(["key", "Return"], run=run)  # send
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.warning("%s injection failed: %s", command, exc)
        return False


def toggle_remote_control(cwd: str, *, run=subprocess.run, pause=time.sleep) -> bool:
    return _slash_command(cwd, "/remote-control", run=run, pause=pause)


def compact_session(cwd: str, *, run=subprocess.run, pause=time.sleep) -> bool:
    """Manually compact the session's context (the phone app cannot)."""
    return _slash_command(cwd, "/compact", run=run, pause=pause)
