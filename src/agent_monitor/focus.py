"""Focus the desktop window belonging to a session (X11, wmctrl)."""

from __future__ import annotations

import logging
import re
import subprocess
import time

from .render import project_name

_LOGGER = logging.getLogger(__name__)

# VS Code builds its title as "<editor> - <folder> - Visual Studio Code" with the
# default `window.title` separator, so the folder is always one whole segment.
_SEPARATOR = " - "
_VSCODE = "visual studio code"

# A segment can carry a decoration: "myws (Workspace)", "proj [SSH: host]".
_DECORATION = re.compile(r"\s*[(\[][^)\]]*[)\]]\s*$")

# How long to wait for the window manager to actually hand focus over. GNOME's
# focus-stealing prevention can highlight the taskbar entry instead of raising the
# window while wmctrl still exits 0 — only the active window tells the truth.
_POLL_ATTEMPTS = 12
_POLL_SECONDS = 0.05


def _titled_for(name: str) -> re.Pattern[str]:
    """Match `name` only as a whole word in a window title.

    A plain substring test cannot tell `game` from `game_uploader`, and the callers do
    more than raise a window — they type into whatever they focused. So the name has to
    be bounded by something that is not part of a longer identifier: `~/code/game`
    matches, `game_uploader` and `mini-game` do not.
    """
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE)


def _rank(title: str, name: str, titled: re.Pattern[str]) -> int | None:
    """How well a window title identifies this session. Lower is better, None = not it."""
    is_vscode = _VSCODE in title.lower()
    segments = (_DECORATION.sub("", seg.strip()) for seg in title.split(_SEPARATOR))
    if any(seg.lower() == name.lower() for seg in segments):
        return 0 if is_vscode else 1
    if is_vscode:
        # A VS Code window always carries its folder as a segment, so a hit anywhere
        # else in the title is another project's session title happening to mention
        # ours ("Add game uploader support - youtube - Visual Studio Code").
        return None
    # Terminals title themselves with the path instead: "aaron@host: ~/code/game".
    return 2 if titled.search(title) else None


def _active_window(run) -> int | None:
    """The focused window id, or None when it cannot be determined at all."""
    try:
        res = run(["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.debug("cannot read the active window: %s", exc)
        return None
    if getattr(res, "returncode", 0) != 0:
        return None
    try:
        return int((res.stdout or "").strip())
    except ValueError:
        return None


def _focus_landed(target: str, *, run, pause) -> bool | None:
    """Did `target` actually become the active window? None = cannot tell."""
    want = int(target, 16)
    for _ in range(_POLL_ATTEMPTS):
        active = _active_window(run)
        if active is None:
            return None  # no xdotool: fall back to trusting wmctrl's exit code
        if active == want:
            return True
        pause(_POLL_SECONDS)
    return False


def focus_window(cwd: str, *, run=subprocess.run, pause=time.sleep) -> bool:
    """Raise the window belonging to this session, preferring its VS Code window."""
    name = project_name(cwd)
    if not name:
        return False
    try:
        out = run(["wmctrl", "-l"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.warning("wmctrl unavailable: %s", exc)
        return False
    titled = _titled_for(name)
    candidates = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4:
            continue
        rank = _rank(parts[3], name, titled)
        if rank is not None:
            candidates.append((rank, parts[0]))
    if not candidates:
        _LOGGER.info("no window matching %r", name)
        return False
    # Sort on the rank alone: Python's sort is stable, so windows of equal rank keep
    # wmctrl's order instead of being tie-broken on the window id, which is arbitrary.
    candidates.sort(key=lambda c: c[0])
    target = candidates[0][1]
    try:
        result = run(["wmctrl", "-i", "-a", target], timeout=2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.warning("wmctrl focus failed: %s", exc)
        return False
    # Neither of the next two checks is cosmetic: restart_session and the slash-command
    # actions type into whatever holds focus the moment we report the window is up.
    if getattr(result, "returncode", 0) != 0:
        _LOGGER.warning("wmctrl could not raise %s for %r — window gone?", target, name)
        return False
    if _focus_landed(target, run=run, pause=pause) is False:
        _LOGGER.warning("the window manager did not raise %s for %r", target, name)
        return False
    return True
