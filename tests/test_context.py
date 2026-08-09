import json
from datetime import datetime, timezone

from agent_monitor import context


def test_model_short_strips_prefix_and_date():
    assert context.model_short("claude-fable-5") == "fable-5"
    assert context.model_short("claude-haiku-4-5-20251001") == "haiku-4-5"
    assert context.model_short("claude-sonnet-4-5-20250929[1m]") == "sonnet-4-5[1m]"
    assert context.model_short("") == ""


def _write_session(claude_dir, pid, session_id, cwd):
    sessions = claude_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(
        json.dumps({"pid": pid, "sessionId": session_id, "cwd": cwd})
    )


def _write_transcript(claude_dir, slug, session_id, lines):
    proj = claude_dir / "projects" / slug
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in lines))
    return path


def _assistant(tokens_in, cache_read, cache_create, model="claude-fable-5",
               effort="high", sidechain=False):
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "effort": effort,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": tokens_in,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
                "output_tokens": 10,
            },
        },
    }


def test_transcript_path_from_pid(tmp_path):
    _write_session(tmp_path, 123, "abc-def", "/home/x/my_proj")
    p = _write_transcript(tmp_path, "-home-x-my-proj", "abc-def", [])
    assert context.transcript_path(123, claude_dir=tmp_path) == p


def test_transcript_path_falls_back_to_glob(tmp_path):
    # cwd changed since session start -> slug doesn't match, glob finds it
    _write_session(tmp_path, 123, "abc-def", "/home/x/elsewhere")
    p = _write_transcript(tmp_path, "-home-x-my-proj", "abc-def", [])
    assert context.transcript_path(123, claude_dir=tmp_path) == p


def test_transcript_path_missing_session_file(tmp_path):
    assert context.transcript_path(999, claude_dir=tmp_path) is None


def test_read_context_takes_last_main_assistant_entry(tmp_path):
    path = _write_transcript(tmp_path, "-p", "s", [
        _assistant(2, 10_000, 1_000, effort="low"),
        {"type": "user", "message": {"role": "user"}},
        _assistant(2, 90_000, 8_000, effort="xhigh"),
        _assistant(1, 5_000, 100, sidechain=True),   # subagent — not main context
        {"type": "system"},
    ])
    info = context.read_context(path)
    assert info.model == "fable-5"
    assert info.effort == "xhigh"
    assert info.percent == round((2 + 90_000 + 8_000) * 100 / 200_000)


def test_read_context_1m_window_from_model_tag(tmp_path):
    path = _write_transcript(tmp_path, "-p", "s", [
        _assistant(0, 400_000, 0, model="claude-sonnet-4-5-20250929[1m]"),
    ])
    assert context.read_context(path).percent == 40


def test_read_context_1m_window_from_token_evidence(tmp_path):
    # the 1m beta rides a header — the model string stays plain, but >200k
    # observed tokens prove the large window
    path = _write_transcript(tmp_path, "-p", "s", [
        _assistant(0, 400_000, 0, model="claude-opus-5"),
    ])
    assert context.read_context(path).percent == 40


def test_read_context_1m_window_from_earlier_entry_evidence(tmp_path):
    # an earlier turn exceeded 200k: the window stays large even though the
    # last turn is small again (e.g. after tool-heavy turns)
    path = _write_transcript(tmp_path, "-p", "s", [
        _assistant(0, 400_000, 0, model="claude-opus-5"),
        _assistant(0, 90_000, 10_000, model="claude-opus-5"),
    ])
    assert context.read_context(path).percent == 10


def test_read_context_1m_window_from_settings_default(tmp_path):
    # user's settings.json pins e.g. "claude-fable-5[1m]": sessions on that
    # model run large even when the transcript shows no >200k evidence yet
    path = _write_transcript(tmp_path, "-p", "s", [
        _assistant(0, 90_000, 10_000, model="claude-fable-5"),
    ])
    assert context.read_context(path, large_for="fable-5").percent == 10
    assert context.read_context(path, large_for="opus-5").percent == 50
    assert context.read_context(path).percent == 50


def test_settings_large_model(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"model": "claude-fable-5[1m]"}))
    assert context.settings_large_model(claude_dir=tmp_path) == "fable-5"
    (tmp_path / "settings.json").write_text(json.dumps({"model": "claude-opus-5"}))
    assert context.settings_large_model(claude_dir=tmp_path) is None
    assert context.settings_large_model(claude_dir=tmp_path / "nope") is None


def test_read_context_skips_synthetic_and_handles_garbage(tmp_path):
    path = _write_transcript(tmp_path, "-p", "s", [
        _assistant(2, 60_000, 0),
        _assistant(0, 0, 0, model="<synthetic>"),
    ])
    with open(path, "a") as f:
        f.write("not json at all\n")
    assert context.read_context(path).percent == 30


def test_read_context_empty_or_missing(tmp_path):
    path = _write_transcript(tmp_path, "-p", "s", [{"type": "user"}])
    assert context.read_context(path) is None
    assert context.read_context(tmp_path / "nope.jsonl") is None


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _write_usage(tmp_path, five_hour, seven_day):
    p = tmp_path / "claude.json"
    p.write_text(json.dumps({
        "cachedUsageUtilization": {
            "fetchedAtMs": 0,
            "utilization": {"five_hour": five_hour, "seven_day": seven_day},
        }
    }))
    return p


def test_read_usage_parses_both_limits(tmp_path):
    p = _write_usage(
        tmp_path,
        {"utilization": 100, "resets_at": "2026-08-09T13:40:00+00:00"},
        {"utilization": 71, "resets_at": "2026-08-11T14:00:00+00:00"},
    )
    five, seven = context.read_usage(p, now=NOW)
    assert (five.label, five.percent, five.stale) == ("5h", 100, False)
    assert five.resets_at == "13:40"       # same day: time only
    assert (seven.label, seven.percent) == ("7d", 71)
    assert seven.resets_at == "Tue 14:00"  # other day: weekday + time


def test_read_usage_marks_passed_reset_as_stale(tmp_path):
    p = _write_usage(
        tmp_path,
        {"utilization": 100, "resets_at": "2026-08-09T11:40:00+00:00"},
        None,
    )
    (five,) = context.read_usage(p, now=NOW)
    assert five.stale is True


def test_read_usage_missing_file_or_key(tmp_path):
    assert context.read_usage(tmp_path / "nope.json", now=NOW) == []
    p = tmp_path / "claude.json"
    p.write_text("{}")
    assert context.read_usage(p, now=NOW) == []


def _cache_file(tmp_path, fetched_at_ms, five_hour):
    p = tmp_path / "claude.json"
    p.write_text(json.dumps({
        "cachedUsageUtilization": {
            "fetchedAtMs": fetched_at_ms,
            "utilization": {"five_hour": five_hour, "seven_day": None},
        }
    }))
    return p


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_provider_prefers_fresh_cache_without_fetching(tmp_path):
    # cache fetched 60s ago, reset in the future -> no network call
    now = NOW  # 2026-08-09 12:00 UTC
    p = _cache_file(tmp_path, (now.timestamp() - 60) * 1000,
                    {"utilization": 40, "resets_at": "2026-08-09T13:00:00+00:00"})
    calls = []
    provider = context.UsageProvider(
        cache_path=p, fetch_fn=lambda: calls.append(1) or {},
        time_fn=_Clock(now.timestamp()), now_fn=lambda: now)
    (five,) = provider()
    assert (five.percent, five.stale) == (40, False)
    assert calls == []


def test_provider_fetches_live_when_cache_stale(tmp_path):
    # cache says the reset already passed -> live fetch wins
    now = NOW
    p = _cache_file(tmp_path, (now.timestamp() - 3600) * 1000,
                    {"utilization": 100, "resets_at": "2026-08-09T11:40:00+00:00"})
    live = {"five_hour": {"utilization": 35, "resets_at": "2026-08-09T16:50:00+00:00"}}
    provider = context.UsageProvider(
        cache_path=p, fetch_fn=lambda: live,
        time_fn=_Clock(now.timestamp()), now_fn=lambda: now)
    (five,) = provider()
    assert (five.percent, five.stale) == (35, False)


def test_provider_throttles_fetch_and_refetches_when_aged(tmp_path):
    now = NOW
    clock = _Clock(now.timestamp())
    p = _cache_file(tmp_path, (now.timestamp() - 3600) * 1000,
                    {"utilization": 100, "resets_at": "2026-08-09T11:40:00+00:00"})
    calls = []
    live = {"five_hour": {"utilization": 35, "resets_at": "2026-08-09T16:50:00+00:00"}}
    provider = context.UsageProvider(
        cache_path=p, fetch_fn=lambda: calls.append(1) or live,
        time_fn=clock, now_fn=lambda: now)
    provider()
    provider()          # immediately again: live snapshot still fresh
    assert len(calls) == 1
    clock.t += context.LIVE_MAX_AGE_S + 1
    provider()          # snapshot aged out -> refetch
    assert len(calls) == 2


def test_provider_failure_falls_back_to_cache(tmp_path):
    now = NOW
    p = _cache_file(tmp_path, (now.timestamp() - 3600) * 1000,
                    {"utilization": 100, "resets_at": "2026-08-09T11:40:00+00:00"})
    provider = context.UsageProvider(
        cache_path=p, fetch_fn=lambda: None,
        time_fn=_Clock(now.timestamp()), now_fn=lambda: now)
    (five,) = provider()
    assert (five.percent, five.stale) == (100, True)  # honest stale cache


def test_fetch_usage_util_refuses_expired_or_missing_creds(tmp_path):
    creds = tmp_path / "creds.json"
    assert context.fetch_usage_util(creds_path=creds) is None
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "x", "expiresAt": 1000}}))  # long expired
    assert context.fetch_usage_util(creds_path=creds) is None


def _ask(tool="AskUserQuestion", resolved=False, tail_extra=()):
    entries = [
        _assistant(2, 50_000, 0),
        {"type": "user", "isSidechain": False, "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "isSidechain": False, "effort": "high", "message": {
            "model": "claude-fable-5",
            "usage": {"input_tokens": 2, "cache_read_input_tokens": 60_000,
                      "cache_creation_input_tokens": 0, "output_tokens": 5},
            "content": [{"type": "tool_use", "id": "tu_1", "name": tool, "input": {}}],
        }},
    ]
    if resolved:
        entries.append({"type": "user", "isSidechain": False, "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "answer"}]}})
    entries.extend(tail_extra)
    return entries


def test_pending_question_detected(tmp_path):
    path = _write_transcript(tmp_path, "-p", "s", _ask())
    assert context.read_context(path).question is True


def test_pending_plan_approval_detected(tmp_path):
    path = _write_transcript(tmp_path, "-p", "s", _ask(tool="ExitPlanMode"))
    assert context.read_context(path).question is True


def test_answered_question_not_pending(tmp_path):
    path = _write_transcript(tmp_path, "-p", "s", _ask(resolved=True))
    assert context.read_context(path).question is False


def test_running_tool_is_not_a_question(tmp_path):
    # unresolved Bash/Agent at the tail = tool still running, NOT blocked
    path = _write_transcript(tmp_path, "-p", "s", _ask(tool="Bash"))
    assert context.read_context(path).question is False


def test_bookkeeping_entries_do_not_hide_a_pending_question(tmp_path):
    # ai-title / last-prompt / queue-operation lines appear while pending
    extra = ({"type": "last-prompt"}, {"type": "ai-title"},
             {"type": "queue-operation"},
             {"type": "assistant", "isSidechain": True, "message": {"role": "assistant"}})
    path = _write_transcript(tmp_path, "-p", "s", _ask(tail_extra=extra))
    assert context.read_context(path).question is True


def test_interrupted_question_not_pending(tmp_path):
    # Esc during the question leaves no tool_result, only a user text entry
    extra = ({"type": "user", "isSidechain": False, "message": {
        "role": "user", "content": "[Request interrupted by user]"}},)
    path = _write_transcript(tmp_path, "-p", "s", _ask(tail_extra=extra))
    assert context.read_context(path).question is False
