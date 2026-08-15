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
    question: bool = False  # transcript shows an unanswered AskUserQuestion/plan
    #                         approval — renders red like WAITING (VS Code fires
    #                         no Notification hook, this path needs no hooks)
    blocked: bool = False   # stopped on a usage-limit notice: needs a model
    #                         switch or credits before it can continue (red)
    waiting_for: str = ""   # name of a peer session it dispatched work to and
    #                         whose reply is still outstanding (yellow, never green)
    host: str = ""          # "" = this machine; otherwise the SSH host whose
    #                         probe reports it (its pid is not ours to check)
    hidden_at: float = 0.0  # when the user took this session off the deck; it
    #                         keeps running and returns on its next activity

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
            "question": self.question,
            "blocked": self.blocked,
            "waiting_for": self.waiting_for,
            "host": self.host,
            "hidden_at": self.hidden_at,
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
            question=bool(d.get("question", False)),
            blocked=bool(d.get("blocked", False)),
            waiting_for=str(d.get("waiting_for", "")),
            host=str(d.get("host", "")),
            hidden_at=float(d.get("hidden_at", 0.0)),
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
    if event == "PermissionRequest":
        return Status.WAITING
    if event == "PermissionDenied":
        return Status.BUSY  # Claude received the denial and keeps working
    if event == "PostToolUse":
        # Fires after an approved tool ran — only ever CLEARS a waiting state.
        return Status.BUSY if current is Status.WAITING else None
    return None
