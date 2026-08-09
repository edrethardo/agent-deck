"""Per-session context/model/effort and account usage, read from Claude
Code's own files (~/.claude/sessions/<pid>.json, the session transcript,
and the usage cache in ~/.claude.json). Read-only — nothing here writes."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_JSON = Path.home() / ".claude.json"
TAIL_BYTES = 262_144  # transcripts append; the last assistant entry is near the end
DEFAULT_WINDOW = 200_000
LARGE_WINDOW = 1_000_000  # models tagged [1m]


@dataclass(frozen=True)
class ContextInfo:
    percent: int  # of the model's context window
    model: str    # short display form, e.g. "fable-5"
    effort: str   # reasoning effort, "" if not recorded


@dataclass(frozen=True)
class UsageLimit:
    label: str      # "5h" / "7d"
    percent: int
    resets_at: str  # local "13:40" (today) or "Tue 14:00", "" if unknown
    stale: bool     # reset time already passed but the cache wasn't refreshed


def model_short(model: str) -> str:
    model = re.sub(r"^claude-", "", model)
    return re.sub(r"-20\d{6}(?=\[|$)", "", model)


def transcript_path(pid: int, claude_dir: Path | None = None) -> Path | None:
    """Transcript of the session owned by claude process `pid`, or None."""
    claude_dir = claude_dir or CLAUDE_DIR
    try:
        meta = json.loads((claude_dir / "sessions" / f"{pid}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    session_id = meta.get("sessionId")
    if not session_id:
        return None
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(meta.get("cwd", "")))
    path = claude_dir / "projects" / slug / f"{session_id}.jsonl"
    if path.exists():
        return path
    hits = sorted((claude_dir / "projects").glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def settings_large_model(claude_dir: Path | None = None) -> str | None:
    """Short name of the user's default model IF it is pinned to the 1m
    window in ~/.claude/settings.json (e.g. "claude-fable-5[1m]"), else None."""
    claude_dir = claude_dir or CLAUDE_DIR
    try:
        model = json.loads((claude_dir / "settings.json").read_text()).get("model") or ""
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(model, str) or not model.endswith("[1m]"):
        return None
    return model_short(model.removesuffix("[1m]"))


def read_context(path: Path, large_for: str | None = None) -> ContextInfo | None:
    """Context info from the last main-thread assistant entry in `path`.

    Window size: 200k by default, 1m when proven. The 1m beta rides an HTTP
    header, so the transcript's model string usually stays plain — evidence
    is a "[1m]" tag, any observed turn above 200k tokens, or the session
    running the user's settings-pinned 1m model (`large_for`)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read()
    except OSError:
        return None
    last: tuple[int, str, str] | None = None  # (tokens, model, effort)
    max_tokens = 0
    for raw in tail.splitlines():
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        if entry.get("isSidechain"):
            continue  # subagent turn — not the main conversation's context
        message = entry.get("message") or {}
        usage = message.get("usage") or {}
        model = str(message.get("model") or "")
        if not usage or model == "<synthetic>":
            continue
        tokens = sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        max_tokens = max(max_tokens, tokens)
        last = (tokens, model, str(entry.get("effort") or ""))
    if last is None:
        return None
    tokens, model, effort = last
    short = model_short(model)
    large = "[1m]" in model or max_tokens > DEFAULT_WINDOW or (large_for is not None and short == large_for)
    window = LARGE_WINDOW if large else DEFAULT_WINDOW
    return ContextInfo(
        percent=min(100, round(tokens * 100 / window)),
        model=short,
        effort=effort,
    )


def session_context(pid: int, claude_dir: Path | None = None) -> ContextInfo | None:
    path = transcript_path(pid, claude_dir)
    if path is None:
        return None
    return read_context(path, large_for=settings_large_model(claude_dir))


def read_usage(path: Path | None = None, *, now: datetime | None = None) -> list[UsageLimit]:
    """Account usage limits as cached by Claude Code (what /usage shows)."""
    path = path or CLAUDE_JSON
    now = now or datetime.now().astimezone()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    util = (data.get("cachedUsageUtilization") or {}).get("utilization") or {}
    out = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        entry = util.get(key)
        if not isinstance(entry, dict) or entry.get("utilization") is None:
            continue
        resets_txt, stale = "", False
        raw = entry.get("resets_at")
        if raw:
            try:
                resets = datetime.fromisoformat(raw).astimezone(now.tzinfo)
            except ValueError:
                resets = None
            if resets is not None:
                stale = resets < now
                resets_txt = resets.strftime("%H:%M" if resets.date() == now.date() else "%a %H:%M")
        out.append(UsageLimit(label, int(entry["utilization"]), resets_txt, stale))
    return out
