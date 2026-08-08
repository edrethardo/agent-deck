"""Discovery of running Claude Code processes via /proc."""

from __future__ import annotations

import os
from pathlib import Path

_MAX_WALK = 15


def _iter_pids() -> list[int]:
    return [int(e) for e in os.listdir("/proc") if e.isdigit()]


def _same_uid(pid: int) -> bool:
    try:
        return os.stat(f"/proc/{pid}").st_uid == os.getuid()
    except OSError:
        return False


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _ppid(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def _cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def looks_like_claude(argv: list[str]) -> bool:
    """True if the first argv entries point at the claude binary/CLI."""
    return any("claude" in part.lower() for part in argv[:2])


def claude_ancestor_pid(start_pid: int) -> int:
    """Nearest ancestor of start_pid (inclusive) that looks like claude.

    The hook may be spawned via a short-lived shell wrapper (observed with
    the VS Code extension); recording that wrapper's PID gets the session
    pruned as dead. Falls back to start_pid if no claude ancestor is found.
    """
    pid = start_pid
    for _ in range(_MAX_WALK):
        if pid <= 1:
            break
        if looks_like_claude(_cmdline(pid)):
            return pid
        pid = _ppid(pid)
    return start_pid


def claude_processes() -> dict[int, str]:
    """PID -> cwd for leaf-most running claude processes of this user.

    Leaf-most: an entry whose descendant also matches is dropped (the VS Code
    extension host contains "claude" in its extension path but the per-session
    CLI child is the process we want).
    """
    matches: dict[int, str] = {}
    for pid in _iter_pids():
        if not _same_uid(pid):
            continue
        if not looks_like_claude(_cmdline(pid)):
            continue
        cwd = _cwd(pid)
        if cwd is None:
            continue
        matches[pid] = cwd
    result = dict(matches)
    for pid in matches:
        anc = _ppid(pid)
        for _ in range(_MAX_WALK):
            if anc <= 1:
                break
            if anc in matches:
                result.pop(anc, None)
            anc = _ppid(anc)
    return result
