"""Per-session context/model/effort and account usage, read from Claude
Code's own files (~/.claude/sessions/<pid>.json, the session transcript,
and the usage cache in ~/.claude.json). Read-only — nothing here writes."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_JSON = Path.home() / ".claude.json"
CREDENTIALS = CLAUDE_DIR / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CACHE_MAX_AGE_S = 600.0   # trust Claude Code's own cache this long
LIVE_MAX_AGE_S = 600.0    # refetch our live snapshot after this
FETCH_RETRY_S = 120.0     # back off after a failed live fetch
TAIL_BYTES = 262_144  # transcripts append; the last assistant entry is near the end
DEFAULT_WINDOW = 200_000
LARGE_WINDOW = 1_000_000  # models tagged [1m]


@dataclass(frozen=True)
class ContextInfo:
    percent: int  # of the model's context window
    model: str    # short display form, e.g. "fable-5"
    effort: str   # reasoning effort, "" if not recorded
    question: bool = False  # an AskUserQuestion/plan approval awaits the user
    activity: float = 0.0   # newest mtime of transcript + subagent files —
    #                         autonomous re-invocations and background agents
    #                         fire no UserPromptSubmit, only the files move
    blocked: bool = False   # ended on a usage/rate-limit message: the session
    #                         cannot continue without the user (model switch,
    #                         credits) and no hook reports it
    peer_name: str = ""     # a peer session it sent work to and awaits
    peer_pid: int = 0       # that peer's pid, resolved via the sessions dir


# Tools that block on USER input by design — an unresolved call at the tail of
# the transcript means the session is waiting for a human. Ordinary tools
# (Bash, Agent, ...) stay unresolved for minutes while simply RUNNING, so they
# must never count.
BLOCKING_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})

# A turn that consists of nothing but a short limit notice — the session is
# stuck until the user switches model or buys credits. Matched only against a
# lone short text block, and the notice must OPEN the message: prose that
# merely mentions limits ("when you've reached your weekly limit, the bar
# turns red") must never light a key red.
LIMIT_START_RE = re.compile(
    r"\s*(claude\s+)?((you'?ve|you have)\s+reached\s+your\b.{0,40}\blimit"
    r"|usage\s+limit\s+reached)",
    re.IGNORECASE)
# Actionable markers only Claude Code's own notice carries.
LIMIT_MARKER_RE = re.compile(r"(/usage-credits|limit will reset at)", re.IGNORECASE)
MAX_LIMIT_MSG_LEN = 400
SHORT_NOTICE_LEN = 200
PEER_WAIT_WINDOW_S = 1800.0  # a dispatched peer task is "in flight" this long
# Replies address the peer by its messaging socket instead of its name.
UDS_PID_RE = re.compile(r"cc-socks/(\d+)\.sock")


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


def _entry_epoch(entry: dict) -> float:
    raw = entry.get("timestamp")
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _is_limit_notice(entry: dict) -> bool:
    """True if this assistant turn is nothing but a usage-limit notice."""
    content = (entry.get("message") or {}).get("content") or []
    blocks = [b for b in content if isinstance(b, dict) and b.get("type") != "thinking"]
    if len(blocks) != 1 or blocks[0].get("type") != "text":
        return False
    text = str(blocks[0].get("text") or "")
    if len(text) > MAX_LIMIT_MSG_LEN:
        return False
    if LIMIT_START_RE.match(text):
        return True
    return len(text) <= SHORT_NOTICE_LEN and LIMIT_MARKER_RE.search(text) is not None


def session_names(claude_dir: Path | None = None) -> dict[str, int]:
    """Session name (as ListAgents/SendMessage use it) -> pid."""
    claude_dir = claude_dir or CLAUDE_DIR
    out: dict[str, int] = {}
    for path in (claude_dir / "sessions").glob("*.json"):
        try:
            meta = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        name, pid = meta.get("name"), meta.get("pid")
        if name and isinstance(pid, int):
            out[str(name)] = pid
    return out


def read_context(path: Path, large_for: str | None = None,
                 now: float | None = None) -> ContextInfo | None:
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
    now = now if now is not None else time.time()
    last: tuple[int, str, str] | None = None  # (tokens, model, effort)
    max_tokens = 0
    tail_entry: dict | None = None  # last main-chain user/assistant entry
    peer_name = ""
    for raw in tail.splitlines():
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue  # subagent turn — not the main conversation's context
        if entry.get("type") in ("user", "assistant"):
            # bookkeeping lines (ai-title, queue-operation, ...) don't advance
            # the conversation and must not mask a pending question
            tail_entry = entry
        if entry.get("type") != "assistant":
            continue
        for blk in (entry.get("message") or {}).get("content") or []:
            # Work handed to a peer session: SendMessage returns immediately,
            # so nothing else in THIS session marks the wait for the reply.
            if isinstance(blk, dict) and blk.get("type") == "tool_use" \
                    and blk.get("name") == "SendMessage":
                if now - _entry_epoch(entry) <= PEER_WAIT_WINDOW_S:
                    to = str((blk.get("input") or {}).get("to") or "")
                    peer_name = to.split(" [")[0].strip()
                else:
                    peer_name = ""
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
    question = blocked = False
    if tail_entry is not None and tail_entry.get("type") == "assistant":
        blocked = _is_limit_notice(tail_entry)
        # A resolved call would be followed by a user(tool_result) entry, an
        # interrupted one by a user text entry — so pending means: the very
        # last conversation entry is an assistant message calling a tool that
        # only a human can answer.
        for blk in (tail_entry.get("message") or {}).get("content") or []:
            if isinstance(blk, dict) and blk.get("type") == "tool_use" \
                    and blk.get("name") in BLOCKING_TOOLS:
                question = True
                break
    tokens, model, effort = last
    short = model_short(model)
    large = "[1m]" in model or max_tokens > DEFAULT_WINDOW or (large_for is not None and short == large_for)
    window = LARGE_WINDOW if large else DEFAULT_WINDOW
    return ContextInfo(
        percent=min(100, round(tokens * 100 / window)),
        model=short,
        effort=effort,
        question=question,
        blocked=blocked,
        peer_name=peer_name,
    )


def session_activity(path: Path) -> float:
    """Newest write anywhere in the session: main transcript or the
    subagents' files under <project>/<session_id>/subagents/."""
    try:
        latest = path.stat().st_mtime
    except OSError:
        return 0.0
    for sub in (path.parent / path.stem / "subagents").glob("agent-*.jsonl"):
        try:
            latest = max(latest, sub.stat().st_mtime)
        except OSError:
            continue
    return latest


def session_context(pid: int, claude_dir: Path | None = None,
                    now: float | None = None) -> ContextInfo | None:
    path = transcript_path(pid, claude_dir)
    if path is None:
        return None
    info = read_context(path, large_for=settings_large_model(claude_dir), now=now)
    if info is None:
        return None
    peer_name, peer_pid = info.peer_name, 0
    if peer_name:
        uds = UDS_PID_RE.search(peer_name)
        if uds:
            peer_pid = int(uds.group(1))
            peer_name = f"session {peer_pid}"
            # prefer the readable name if that pid still has one
            for name, pid in session_names(claude_dir).items():
                if pid == peer_pid:
                    peer_name = name
                    break
        else:
            peer_pid = session_names(claude_dir).get(peer_name, 0)
    return dataclasses.replace(info, activity=session_activity(path),
                               peer_name=peer_name, peer_pid=peer_pid)


def _parse_utilization(util: dict, now: datetime) -> list[UsageLimit]:
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


def _read_cache(path: Path | None) -> tuple[dict, float]:
    """Claude Code's cached utilization + its fetch time (0.0 if unusable)."""
    try:
        data = json.loads((path or CLAUDE_JSON).read_text())
    except (OSError, json.JSONDecodeError):
        return {}, 0.0
    cached = data.get("cachedUsageUtilization") or {}
    return cached.get("utilization") or {}, float(cached.get("fetchedAtMs") or 0) / 1000


def read_usage(path: Path | None = None, *, now: datetime | None = None) -> list[UsageLimit]:
    """Account usage limits as cached by Claude Code (what /usage shows)."""
    util, _ = _read_cache(path)
    return _parse_utilization(util, now or datetime.now().astimezone())


def fetch_usage_util(creds_path: Path | None = None, timeout: float = 5.0) -> dict | None:
    """Live usage from the same endpoint the phone app uses, authenticated
    with the user's existing Claude Code OAuth token (read-only; the token
    goes to api.anthropic.com only and is never logged)."""
    try:
        creds = json.loads(Path(creds_path or CREDENTIALS).read_text()).get("claudeAiOauth") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    token = creds.get("accessToken")
    if not token or float(creds.get("expiresAt") or 0) / 1000 <= time.time():
        return None  # expired token: don't burn a request; CC renews it itself
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (OSError, ValueError) as exc:
        _LOGGER.debug("live usage fetch failed: %s", exc)
        return None
    return data if isinstance(data, dict) else None


class UsageProvider:
    """Freshest available account usage: Claude Code's cache while it is
    recent, otherwise a throttled live fetch (the cache only refreshes on
    local UI activity — phone-driven remote-control sessions never touch it)."""

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        creds_path: Path | None = None,
        fetch_fn=None,
        time_fn=time.time,
        now_fn=None,
    ):
        self._cache_path = cache_path
        self._fetch = fetch_fn or (lambda: fetch_usage_util(creds_path=creds_path))
        self._time = time_fn
        self._now = now_fn or (lambda: datetime.now().astimezone())
        self._live_util: dict | None = None
        self._live_at = 0.0
        self._attempt_at = 0.0

    def __call__(self) -> list[UsageLimit]:
        now = self._now()
        t = self._time()
        cache_util, fetched_s = _read_cache(self._cache_path)
        cache_limits = _parse_utilization(cache_util, now)
        if (cache_limits and t - fetched_s < CACHE_MAX_AGE_S
                and not any(lim.stale for lim in cache_limits)):
            return cache_limits
        if t - self._live_at >= LIVE_MAX_AGE_S and t - self._attempt_at >= FETCH_RETRY_S:
            self._attempt_at = t
            util = self._fetch()
            if util:
                self._live_util, self._live_at = util, t
            else:
                _LOGGER.info("live usage fetch unavailable — showing cached data")
        if self._live_util:
            live = _parse_utilization(self._live_util, now)
            if live:
                return live
        return cache_limits
