from agent_monitor import actions


def _fake_run(calls):
    def run(cmd, **kwargs):
        calls.append(cmd)
    return run


def _fake_pause(pauses):
    def pause(seconds):
        pauses.append(seconds)
    return pause


def test_restart_session_sends_reload_window_command(monkeypatch):
    monkeypatch.setattr(actions, "focus_window", lambda cwd, **kw: True)
    monkeypatch.setattr(actions.shutil, "which", lambda name: "/usr/bin/xdotool")
    calls = []
    ok = actions.restart_session("/proj/a", run=_fake_run(calls), pause=_fake_pause([]))
    assert ok is True
    typed = [c for c in calls if c[:2] == ["xdotool", "type"]]
    assert any("Developer: Reload Window" in c for c in typed)


def test_toggle_remote_control_types_slash_command(monkeypatch):
    monkeypatch.setattr(actions, "focus_window", lambda cwd, **kw: True)
    monkeypatch.setattr(actions.shutil, "which", lambda name: "/usr/bin/xdotool")
    calls = []
    ok = actions.toggle_remote_control("/proj/a", run=_fake_run(calls), pause=_fake_pause([]))
    assert ok is True
    typed = [c for c in calls if c[:2] == ["xdotool", "type"]]
    assert any("Claude Code: Focus input" in c for c in typed)
    assert any("/remote-control" in c for c in typed)
    key_calls = [c for c in calls if c[:2] == ["xdotool", "key"]]
    # one Return from the palette itself (executing the picked command) plus
    # the two Returns toggle_remote_control sends after typing the slash command
    assert key_calls.count(["xdotool", "key", "Return"]) == 3


def test_restart_session_returns_false_when_window_not_found(monkeypatch):
    monkeypatch.setattr(actions, "focus_window", lambda cwd, **kw: False)
    monkeypatch.setattr(actions.shutil, "which", lambda name: "/usr/bin/xdotool")
    calls = []
    ok = actions.restart_session("/proj/a", run=_fake_run(calls), pause=_fake_pause([]))
    assert ok is False
    assert calls == []


def test_toggle_remote_control_returns_false_when_window_not_found(monkeypatch):
    monkeypatch.setattr(actions, "focus_window", lambda cwd, **kw: False)
    monkeypatch.setattr(actions.shutil, "which", lambda name: "/usr/bin/xdotool")
    calls = []
    ok = actions.toggle_remote_control("/proj/a", run=_fake_run(calls), pause=_fake_pause([]))
    assert ok is False
    assert calls == []


def test_actions_degrade_cleanly_without_xdotool(monkeypatch):
    monkeypatch.setattr(actions, "focus_window", lambda cwd, **kw: True)
    monkeypatch.setattr(actions.shutil, "which", lambda name: None)
    calls = []
    assert actions.restart_session("/proj/a", run=_fake_run(calls), pause=_fake_pause([])) is False
    assert actions.toggle_remote_control("/proj/a", run=_fake_run(calls), pause=_fake_pause([])) is False
    assert calls == []


def test_palette_injection_error_returns_false(monkeypatch):
    monkeypatch.setattr(actions, "focus_window", lambda cwd, **kw: True)
    monkeypatch.setattr(actions.shutil, "which", lambda name: "/usr/bin/xdotool")

    def boom(cmd, **kwargs):
        raise OSError("no display")

    ok = actions.restart_session("/proj/a", run=boom, pause=_fake_pause([]))
    assert ok is False
