from agent_monitor.statusview import format_duration, read_state, render_status


def _state():
    return {
        "updated": 100.0,
        "sessions": [
            {"session_id": "a", "cwd": "/home/aaron/code/lead-extractor",
             "pid": 1, "status": "waiting", "slot": 0, "since": 40.0},
            {"session_id": "b", "cwd": "/home/aaron/code/graft",
             "pid": 2, "status": "busy", "slot": 1, "since": 90.0},
            {"session_id": "c", "cwd": "/home/aaron/code/over",
             "pid": 3, "status": "available", "slot": None, "since": 95.0},
        ],
    }


def test_render_contains_projects_and_status():
    out = render_status(_state(), now=100.0, daemon_up=True)
    assert "lead-extractor" in out
    assert "waiting for input" in out
    assert "working" in out
    assert "available" in out
    assert "1m0s" in out  # waiting for 60s


def test_overflow_session_shows_dash_for_key():
    out = render_status(_state(), now=100.0, daemon_up=True)
    assert "—" in out


def test_daemon_down_warning():
    out = render_status(None, now=100.0, daemon_up=False)
    assert "daemon is not running" in out


def test_empty_state_message():
    out = render_status({"updated": 1.0, "sessions": []}, now=2.0, daemon_up=True)
    assert "No active sessions" in out


def test_format_duration():
    assert format_duration(5) == "5s"
    assert format_duration(65) == "1m5s"
    assert format_duration(3725) == "1h02m"


def test_terminal_rendering_emits_ansi_colors():
    out = render_status(_state(), now=100.0, daemon_up=True, force_terminal=True)
    assert "\x1b[" in out


def test_bracketed_project_name_renders_verbatim():
    state = {"updated": 1.0, "sessions": [
        {"session_id": "a", "cwd": "/home/aaron/code/[archive]",
         "pid": 1, "status": "busy", "slot": 0, "since": 0.0},
    ]}
    out = render_status(state, now=1.0, daemon_up=True)
    assert "[archive]" in out


def test_read_state_rejects_non_dict(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    from agent_monitor import paths
    paths.state_path().write_text("42")
    assert read_state() is None


def test_unknown_status_label():
    state = {"updated": 1.0, "sessions": [
        {"session_id": "p", "cwd": "/x/scanned", "pid": 9,
         "status": "unknown", "slot": 0, "since": 0.0}]}
    out = render_status(state, now=1.0, daemon_up=True)
    assert "unknown" in out


def test_rc_column_shows_check():
    state = {"updated": 1.0, "sessions": [
        {"session_id": "a", "cwd": "/x/proj", "pid": 1, "status": "available",
         "slot": 0, "since": 0.0, "remote": True}]}
    out = render_status(state, now=1.0, daemon_up=True)
    assert "✓" in out


def test_rc_column_blank_for_non_remote():
    state = {"updated": 1.0, "sessions": [
        {"session_id": "a", "cwd": "/x/proj", "pid": 1, "status": "available",
         "slot": 0, "since": 0.0, "remote": False}]}
    out = render_status(state, now=1.0, daemon_up=True)
    assert "✓" not in out


def test_finished_label_shown_green():
    state = {"updated": 1.0, "sessions": [
        {"session_id": "a", "cwd": "/x/proj", "pid": 1, "status": "available",
         "slot": 0, "since": 0.0, "finished": True}]}
    out = render_status(state, now=1.0, daemon_up=True)
    assert "finished" in out


def test_model_and_context_columns():
    state = {"sessions": [
        {"session_id": "a", "cwd": "/p/x", "pid": 1, "status": "busy",
         "slot": 0, "since": 40.0, "model": "fable-5", "effort": "xhigh",
         "context_pct": 52},
        {"session_id": "b", "cwd": "/p/y", "pid": 2, "status": "busy",
         "slot": 1, "since": 40.0},  # no context data yet
    ]}
    out = render_status(state, now=100.0, daemon_up=True, width=120)
    assert "fable-5 xhigh" in out
    assert "52%" in out
    assert "Model" in out
    assert "Ctx" in out


def test_usage_line_above_table():
    state = {"sessions": [
        {"session_id": "a", "cwd": "/p/x", "pid": 1, "status": "busy",
         "slot": 0, "since": 40.0}],
        "usage": [
            {"label": "5h", "percent": 37, "resets_at": "18:50", "stale": False},
            {"label": "7d", "percent": 74, "resets_at": "Tue 16:00", "stale": True},
        ]}
    out = render_status(state, now=100.0, daemon_up=True, width=120)
    assert "5h 37% (resets 18:50)" in out
    assert "7d --%" in out  # stale: never a knowably wrong number


def test_high_context_marked():
    state = {"sessions": [
        {"session_id": "a", "cwd": "/p/x", "pid": 1, "status": "busy",
         "slot": 0, "since": 40.0, "model": "opus-5", "context_pct": 91}]}
    out = render_status(state, now=100.0, daemon_up=True, width=120)
    assert "91%!" in out


def test_question_shows_waiting_label():
    state = {"sessions": [
        {"session_id": "a", "cwd": "/p/x", "pid": 1, "status": "busy",
         "slot": 0, "since": 40.0, "question": True}]}
    out = render_status(state, now=100.0, daemon_up=True, width=120)
    assert "waiting for input" in out


def test_blocked_and_peer_wait_labels():
    state = {"sessions": [
        {"session_id": "a", "cwd": "/p/x", "pid": 1, "status": "available",
         "slot": 0, "since": 40.0, "blocked": True},
        {"session_id": "b", "cwd": "/p/y", "pid": 2, "status": "available",
         "slot": 1, "since": 40.0, "finished": True, "waiting_for": "peer-9e"},
    ]}
    out = render_status(state, now=100.0, daemon_up=True, width=140)
    assert "limit reached" in out
    assert "waits for peer-9e" in out


def test_remote_session_shows_host_with_project():
    state = {"sessions": [{"session_id": "box:r1", "cwd": "/home/s/proj", "pid": 0,
                           "status": "busy", "slot": 0, "since": 40.0,
                           "host": "spheron-AI-PC"}]}
    out = render_status(state, now=100.0, daemon_up=True, width=140)
    assert "proj @spheron-AI-PC" in out


def test_hidden_session_is_listed_and_marked():
    state = {"sessions": [
        {"session_id": "a", "cwd": "/p/x", "pid": 1, "status": "available",
         "slot": None, "since": 40.0, "hidden_at": 20.0}]}
    out = render_status(state, now=100.0, daemon_up=True, width=140)
    assert "hidden" in out
    assert "x" in out          # still listed: the CLI is the complete view
