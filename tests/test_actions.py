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
    assert [c for c in calls if c[0] == "xdotool"] == []


def test_toggle_remote_control_returns_false_when_window_not_found(monkeypatch):
    monkeypatch.setattr(actions, "focus_window", lambda cwd, **kw: False)
    monkeypatch.setattr(actions.shutil, "which", lambda name: "/usr/bin/xdotool")
    calls = []
    ok = actions.toggle_remote_control("/proj/a", run=_fake_run(calls), pause=_fake_pause([]))
    assert ok is False
    assert [c for c in calls if c[0] == "xdotool"] == []


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


def test_compact_session_types_slash_command(monkeypatch):
    monkeypatch.setattr(actions, "focus_window", lambda cwd, **kw: True)
    monkeypatch.setattr(actions.shutil, "which", lambda name: "/usr/bin/xdotool")
    calls = []
    ok = actions.compact_session("/proj/a", run=_fake_run(calls), pause=_fake_pause([]))
    assert ok is True
    typed = [c for c in calls if c[:2] == ["xdotool", "type"]]
    assert any("Claude Code: Focus input" in c for c in typed)
    assert any("/compact" in c for c in typed)


def test_compact_session_returns_false_when_window_not_found(monkeypatch):
    monkeypatch.setattr(actions, "focus_window", lambda cwd, **kw: False)
    calls = []
    assert actions.compact_session("/proj/a", run=_fake_run(calls), pause=_fake_pause([])) is False
    assert [c for c in calls if c[0] == "xdotool"] == []


class _LoginctlRun:
    def __init__(self, locked):
        self.locked = locked

    def __call__(self, cmd, **kwargs):
        class R:
            stdout = ""
        r = R()
        if cmd[:2] == ["loginctl", "list-sessions"]:
            r.stdout = " 3 1000 aaron seat0 tty2\n"
        elif cmd[:2] == ["loginctl", "show-session"]:
            r.stdout = f"Display=:1\nLockedHint={'yes' if self.locked else 'no'}\n"
        return r


def test_display_locked_parses_loginctl(monkeypatch):
    monkeypatch.setattr(actions.shutil, "which", lambda name: "/usr/bin/loginctl")
    monkeypatch.setenv("DISPLAY", ":1")
    assert actions.display_locked(run=_LoginctlRun(True)) is True
    assert actions.display_locked(run=_LoginctlRun(False)) is False


def test_actions_refuse_when_display_locked(monkeypatch):
    monkeypatch.setattr(actions, "display_locked", lambda **kw: True)
    monkeypatch.setattr(actions, "focus_window", lambda cwd, **kw: True)
    monkeypatch.setattr(actions.shutil, "which", lambda name: "/usr/bin/xdotool")
    calls = []
    assert actions.compact_session("/proj/a", run=_fake_run(calls), pause=_fake_pause([])) is False
    assert actions.restart_session("/proj/a", run=_fake_run(calls), pause=_fake_pause([])) is False
    assert actions.toggle_remote_control("/proj/a", run=_fake_run(calls), pause=_fake_pause([])) is False
    assert calls == []  # nothing typed into the lock screen


def test_end_session_signals_the_pid(monkeypatch):
    sent = []
    monkeypatch.setattr(actions.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert actions.end_session(4242) is True
    import signal
    assert sent == [(4242, 0), (4242, signal.SIGTERM)]   # liveness probe, then stop


def test_end_session_refuses_an_implausible_pid():
    assert actions.end_session(0) is False
    assert actions.end_session(1) is False


def test_end_session_reports_a_dead_or_foreign_process(monkeypatch):
    def kill(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr(actions.os, "kill", kill)
    assert actions.end_session(4242) is False
