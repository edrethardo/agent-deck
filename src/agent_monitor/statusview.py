from __future__ import annotations

import io
import json
import shutil
import socket
import sys
import time

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import paths
from .render import project_name

STATUS_LABEL = {
    "available": ("available", "green"),
    "busy": ("working", "yellow"),
    "waiting": ("waiting for input", "red bold"),
    "unknown": ("unknown", "blue"),
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
        data = json.loads(paths.state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def render_status(state: dict | None, now: float, daemon_up: bool,
                  *, force_terminal: bool = False, width: int | None = 80) -> str:
    if width is None:
        width = shutil.get_terminal_size().columns
    console = Console(file=io.StringIO(), force_terminal=force_terminal, width=width)
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
        label, style = STATUS_LABEL.get(sess.get("status"), (escape(str(sess.get("status"))), ""))
        slot = sess.get("slot")
        key = "—" if slot is None else str(slot + 1)
        table.add_row(
            key,
            escape(project_name(sess.get("cwd", ""))),
            f"[{style}]{label}[/{style}]" if style else label,
            format_duration(now - sess.get("since", now)),
        )
    console.print(table)
    return console.file.getvalue()


def run_status(watch: bool) -> int:
    force = sys.stdout.isatty()
    if not watch:
        print(render_status(read_state(), time.time(), daemon_running(),
                            force_terminal=force, width=None), end="")
        return 0
    try:
        while True:
            out = render_status(read_state(), time.time(), daemon_running(),
                                force_terminal=force, width=None)
            print("\033[2J\033[H" + out, end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
