"""Discovery of running Claude Code processes via /proc."""

from __future__ import annotations

import os
from pathlib import Path

_MAX_WALK = 40


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
    """True if argv points at the claude binary/CLI (not merely mentions it)."""
    if not argv:
        return False
    exe = os.path.basename(argv[0])
    if exe == "claude":
        return True
    if len(argv) > 1 and exe.split("-")[0] in ("node", "bun", "deno", "python3", "python"):
        a1 = argv[1].lower()
        return "claude" in a1 and os.path.basename(a1) in ("cli.js", "claude")
    return False


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
    """PID -> cwd for top-most running claude processes of this user.

    A match whose ancestor also matches is dropped (e.g. a `claude -p` child
    spawned by an interactive session).
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
                result.pop(pid, None)  # child of another claude — keep the outermost
                break
            anc = _ppid(anc)
    return result


ANTHROPIC_NET_PREFIX = "160.79."


def _socket_inodes(pid: int) -> set[str]:
    inodes = set()
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    except OSError:
        pass
    return inodes


def _established_remote_ips(inodes: set[str], tcp_path: str = "/proc/net/tcp") -> list[str]:
    """Remote IPv4 addresses of ESTABLISHED sockets owned by `inodes`."""
    ips = []
    try:
        lines = Path(tcp_path).read_text().splitlines()[1:]
    except OSError:
        return ips
    for line in lines:
        parts = line.split()
        if len(parts) < 10 or parts[3] != "01":  # 01 = ESTABLISHED
            continue
        if parts[9] not in inodes:
            continue
        hex_ip = parts[2].split(":")[0]
        if len(hex_ip) == 8:
            octets = [str(int(hex_ip[i:i + 2], 16)) for i in (6, 4, 2, 0)]
            ips.append(".".join(octets))
    return ips


def has_remote_control(pid: int) -> bool:
    """True if the process holds an established connection into Anthropic's
    network (160.79.0.0/16) — the remote-control relay does, even when idle."""
    inodes = _socket_inodes(pid)
    if not inodes:
        return False
    return any(ip.startswith(ANTHROPIC_NET_PREFIX) for ip in _established_remote_ips(inodes))
