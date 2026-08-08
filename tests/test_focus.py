from types import SimpleNamespace

from agent_monitor.focus import focus_window

WMCTRL_L = """0x04000003  0 host lead_extractor - Visual Studio Code
0x04000009  0 host something unrelated
0x0400000f  0 host lead_extractor — terminal
"""


def _fake_run(calls, stdout=""):
    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout=stdout)
    return run


def test_focus_prefers_vscode_window():
    calls = []
    assert focus_window("/home/aaron/code/lead_extractor", run=_fake_run(calls, WMCTRL_L)) is True
    assert calls[-1] == ["wmctrl", "-i", "-a", "0x04000003"]


def test_focus_no_match_returns_false():
    calls = []
    assert focus_window("/home/aaron/code/nope", run=_fake_run(calls, WMCTRL_L)) is False
    assert len(calls) == 1


def test_focus_missing_wmctrl_returns_false():
    def run(cmd, **kwargs):
        raise OSError("not found")
    assert focus_window("/x/y", run=run) is False
