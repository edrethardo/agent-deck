"""Sessions that live on another machine (VS Code Remote-SSH).

Their process, hooks and transcript are all on the remote host, so nothing
local can observe them. The daemon therefore pipes a self-contained probe
(this package's `context.py` plus a `__main__` block) through `ssh` and reads
the answer as JSON. Nothing is installed on the remote host, and everything
it reads is read-only.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

SSH_TIMEOUT = 20
# Reuse one connection per host: a probe every 30 s must not mean a fresh
# TCP+auth handshake every 30 s.
SSH_OPTS = [
    "-o", "BatchMode=yes",           # never prompt: a locked key just fails
    "-o", "ConnectTimeout=8",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/agent-monitor-%r@%h:%p",
    "-o", "ControlPersist=120",
]

PROBE_MAIN = '''

def _probe_main() -> None:
    import os
    import sys
    import time

    now = time.time()
    out = []
    for meta_path in sorted((CLAUDE_DIR / "sessions").glob("*.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        pid = meta.get("pid")
        if not isinstance(pid, int) or pid <= 1:
            continue
        try:
            os.kill(pid, 0)          # liveness: the remote box owns this pid
        except OSError:
            continue
        info = session_context(pid, now=now)
        if info is None:
            continue
        out.append({
            "pid": pid,
            "session_id": str(meta.get("sessionId") or ""),
            "cwd": str(meta.get("cwd") or ""),
            "name": str(meta.get("name") or ""),
            "model": info.model,
            "effort": info.effort,
            "context_pct": info.percent,
            "question": info.question,
            "blocked": info.blocked,
            "interrupted": info.interrupted,
            "peer_name": info.peer_name,
            # remote-control state has to be read where the sockets are
            "remote": has_remote_control(pid),
            # age, not a timestamp: the two machines' clocks need not agree
            "age": max(0.0, now - info.activity) if info.activity else 1e9,
        })
    json.dump({"sessions": out}, sys.stdout)


_probe_main()
'''


# Both modules are stdlib-only and import nothing from this package, so they
# can simply be concatenated into one script.
PROBE_MODULES = ("context.py", "scan.py")
_FUTURE = "from __future__ import annotations"


def probe_script() -> str:
    """The self-contained probe: our readers + a main that prints JSON."""
    parts = [(Path(__file__).with_name(name)).read_text() for name in PROBE_MODULES]
    body = "\n".join(
        "\n".join(line for line in part.splitlines() if line.strip() != _FUTURE)
        for part in parts
    )
    # a __future__ import is only legal as the first statement of the file
    return f"{_FUTURE}\n{body}{PROBE_MAIN}"


def discover_hosts(run=subprocess.run) -> list[str]:
    """Hosts this machine currently has a VS Code Remote-SSH window open to.

    Probing only these means the daemon never dials a host the user is not
    already connected to."""
    if shutil.which("ssh") is None:
        return []
    try:
        out = run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=5).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return []
    hosts = []
    for line in out.splitlines():
        parts = line.split()
        # Remote-SSH's tunnel process: `ssh -v -T -D <port> ... <host>`
        if not parts or Path(parts[0]).name != "ssh" or "-D" not in parts or "-T" not in parts:
            continue
        host = parts[-1]
        if host.startswith("-") or host in hosts:
            continue
        hosts.append(host)
    return hosts


def probe_host(host: str, run=subprocess.run, script: str | None = None) -> list[dict] | None:
    """Remote sessions on `host`, or None when the probe could not run.

    None means "no idea" and leaves the display untouched — only a successful
    probe may remove that host's sessions."""
    try:
        res = run(["ssh", *SSH_OPTS, host, "python3", "-"],
                  input=script if script is not None else probe_script(),
                  capture_output=True, text=True, timeout=SSH_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        _LOGGER.debug("probe of %s failed: %s", host, exc)
        return None
    if getattr(res, "returncode", 1) != 0:
        _LOGGER.debug("probe of %s exited %s: %s", host, res.returncode,
                      (res.stderr or "").strip()[:200])
        return None
    try:
        data = json.loads(res.stdout or "")
    except ValueError:
        _LOGGER.debug("probe of %s returned no JSON", host)
        return None
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return sessions if isinstance(sessions, list) else None


def probe_all(hosts=None, run=subprocess.run) -> dict[str, list[dict]]:
    """{host: sessions} for every reachable host; failures are omitted."""
    result = {}
    script = probe_script()
    for host in (hosts if hosts is not None else discover_hosts(run=run)):
        sessions = probe_host(host, run=run, script=script)
        if sessions is not None:
            result[host] = sessions
    return result
