from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    AVAILABLE = "available"  # green
    BUSY = "busy"            # yellow
    WAITING = "waiting"      # red
    UNKNOWN = "unknown"      # blue — discovered by process scan, no hook events


@dataclass
class Session:
    session_id: str
    cwd: str
    pid: int
    status: Status
    slot: int | None  # 0-15, None = overflow (no LED)
    since: float
    remote: bool = False
    finished: bool = False  # last transition to AVAILABLE came from a Stop (renders green)
    rc_manual: bool = False  # remote flag was set by a pad toggle — detection can't
    #                          see in-process off-toggles, so the manual value wins
    #                          until the session restarts
    context_pct: int | None = None  # % of the context window used (from the transcript)
    model: str = ""   # short model name, e.g. "fable-5"
    effort: str = ""  # reasoning effort, e.g. "xhigh"

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "pid": self.pid,
            "status": self.status.value,
            "slot": self.slot,
            "since": self.since,
            "remote": self.remote,
            "finished": self.finished,
            "rc_manual": self.rc_manual,
            "context_pct": self.context_pct,
            "model": self.model,
            "effort": self.effort,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            session_id=str(d["session_id"]),
            cwd=str(d.get("cwd", "")),
            pid=int(d.get("pid", 0)),
            status=Status(d["status"]),
            slot=d.get("slot"),
            since=float(d.get("since", 0.0)),
            remote=bool(d.get("remote", False)),
            finished=bool(d.get("finished", False)),
            rc_manual=bool(d.get("rc_manual", False)),
            context_pct=d.get("context_pct"),
            model=str(d.get("model", "")),
            effort=str(d.get("effort", "")),
        )


# Notification texts that do NOT mean the session is blocked (it is just
# sitting idle at the prompt) — per spec the status stays unchanged then.
IDLE_MARKERS = ("waiting for your input",)


def status_for_event(event: str, message: str | None, current: Status | None) -> Status | None:
    """New status for a hook event; None = do not change the status."""
    if event == "SessionStart":
        return Status.AVAILABLE
    if event == "UserPromptSubmit":
        return Status.BUSY
    if event == "Stop":
        return Status.AVAILABLE
    if event == "Notification":
        text = (message or "").lower()
        if any(marker in text for marker in IDLE_MARKERS):
            return None
        return Status.WAITING
    return None
