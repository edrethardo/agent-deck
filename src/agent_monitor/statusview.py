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
from .render import CTX_WARN_PCT, project_name

STATUS_LABEL = {
    "available": ("available", "green"),
    "busy": ("working", "yellow"),
    "waiting": ("waiting for input", "red bold"),
    "unknown": ("unknown", "dark_orange"),
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
    usage = (state or {}).get("usage") or []
    if usage:
        parts = []
        for lim in usage:
            label = escape(str(lim.get("label", "?")))
            if lim.get("stale"):
                parts.append(f"{label} --%")  # reset passed, number knowably wrong
            else:
                txt = f"{label} {lim.get('percent')}%"
                if lim.get("resets_at"):
                    txt += f" (resets {escape(str(lim['resets_at']))})"
                parts.append(txt)
        console.print("Usage: " + "  ·  ".join(parts))
    sessions = (state or {}).get("sessions", [])
    if not sessions:
        console.print("No active sessions.")
        return console.file.getvalue()

    table = Table()
    table.add_column("Key", justify="right")
    table.add_column("Project")
    table.add_column("Status")
    table.add_column("RC", justify="center")
    table.add_column("Model")
    table.add_column("Ctx", justify="right")
    table.add_column("For", justify="right")
    for sess in sessions:
        label, style = STATUS_LABEL.get(sess.get("status"), (escape(str(sess.get("status"))), ""))
        if sess.get("status") == "available" and sess.get("finished"):
            label, style = "finished", "green"
        if sess.get("question"):
            label, style = STATUS_LABEL["waiting"]
        slot = sess.get("slot")
        key = "—" if slot is None else str(slot + 1)
        model = f"{sess.get('model', '')} {sess.get('effort', '')}".strip()
        pct = sess.get("context_pct")
        if pct is None:
            ctx = ""
        elif pct >= CTX_WARN_PCT:
            ctx = f"[red bold]{pct}%![/red bold]"
        else:
            ctx = f"{pct}%"
        table.add_row(
            key,
            escape(project_name(sess.get("cwd", ""))),
            f"[{style}]{label}[/{style}]" if style else label,
            "✓" if sess.get("remote") else "",
            escape(model),
            ctx,
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
