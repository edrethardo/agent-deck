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
