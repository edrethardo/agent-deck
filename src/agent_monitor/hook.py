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
