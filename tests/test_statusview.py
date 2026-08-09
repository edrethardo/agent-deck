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
