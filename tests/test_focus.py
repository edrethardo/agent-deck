from types import SimpleNamespace

from agent_monitor.focus import focus_window

WMCTRL_L = """0x04000003  0 host lead_extractor - Visual Studio Code
0x04000009  0 host something unrelated
0x0400000f  0 host lead_extractor — terminal
"""

# Two sessions whose project names are prefixes of one another, with the window ids
# from the real desktop: `game_uploader`'s id sorts BEFORE `game`'s, so any tie-break
# on the id hands the wrong window to a plain substring match.
PREFIX_COLLISION = """0x0240000b -1 host Brainstorming session - game_uploader - Visual Studio Code
0x024001b3  0 host Brainstorming session wi… - game - Visual Studio Code
"""

# Session titles are Claude-generated prose, so they can name someone else's project.
TITLE_MENTIONS_ANOTHER_PROJECT = """0x03000001  0 host Add game uploader support - youtube - Visual Studio Code
0x03000002  0 host Brainstorming session wi… - game - Visual Studio Code
"""


def _fake_run(calls, stdout="", returncode=0, active="obeys"):
    """Stand in for wmctrl + xdotool.

    `active` models the window manager: "obeys" hands focus to whatever was raised,
    "refuses" never does (GNOME's focus-stealing prevention), and None means xdotool
    cannot answer at all.
    """
    raised = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["xdotool", "getactivewindow"]:
            if active is None:
                return SimpleNamespace(stdout="", returncode=1)
            focused = raised[-1] if (active == "obeys" and raised) else "0x0badf00d"
            return SimpleNamespace(stdout=f"{int(focused, 16)}\n", returncode=0)
        if cmd[:3] == ["wmctrl", "-i", "-a"]:
            if returncode == 0:
                raised.append(cmd[3])
            return SimpleNamespace(stdout="", returncode=returncode)
        return SimpleNamespace(stdout=stdout, returncode=0)

    return run


def _raised(calls):
    """Which window ids were actually handed to `wmctrl -i -a`."""
    return [c[3] for c in calls if c[:3] == ["wmctrl", "-i", "-a"]]


def _nowait(_seconds):
    """Poll without sleeping, so the retry loop costs no wall-clock in tests."""


def _focus(cwd, run):
    return focus_window(cwd, run=run, pause=_nowait)


def test_focus_prefers_vscode_window():
    calls = []
    assert _focus("/home/aaron/code/lead_extractor", _fake_run(calls, WMCTRL_L)) is True
    assert _raised(calls) == ["0x04000003"]


def test_focus_no_match_returns_false():
    calls = []
    assert _focus("/home/aaron/code/nope", _fake_run(calls, WMCTRL_L)) is False
    assert len(calls) == 1


def test_focus_missing_wmctrl_returns_false():
    def run(cmd, **kwargs):
        raise OSError("not found")
    assert focus_window("/x/y", run=run) is False


def test_focus_ignores_a_window_whose_name_merely_starts_with_the_project():
    """`game` must not land on `game_uploader` — the name has to be a whole word."""
    calls = []
    assert _focus("/home/aaron/game", _fake_run(calls, PREFIX_COLLISION)) is True
    assert _raised(calls) == ["0x024001b3"]


def test_focus_still_finds_the_longer_name_itself():
    calls = []
    assert _focus("/home/aaron/code/game_uploader", _fake_run(calls, PREFIX_COLLISION)) is True
    assert _raised(calls) == ["0x0240000b"]


def test_focus_ignores_a_session_title_that_merely_mentions_the_project():
    """Only the folder segment identifies a VS Code window — not the whole title."""
    calls = []
    assert _focus("/home/aaron/game", _fake_run(calls, TITLE_MENTIONS_ANOTHER_PROJECT)) is True
    assert _raised(calls) == ["0x03000002"], "the youtube window merely says 'game uploader'"


def test_focus_reads_through_a_decorated_folder_segment():
    """VS Code decorates the folder for remotes and multi-root workspaces."""
    titles = (
        "0x06000001  0 host when will the dev… - coding-agent [SSH: spheron-AI-PC] - Visual Studio Code\n"
        "0x06000002  0 host main.py - dungeon (Workspace) - Visual Studio Code\n"
    )
    calls = []
    assert _focus("/home/spheron/coding-agent", _fake_run(calls, titles)) is True
    assert _raised(calls) == ["0x06000001"]

    calls = []
    assert _focus("/home/aaron/dungeon", _fake_run(calls, titles)) is True
    assert _raised(calls) == ["0x06000002"]


def test_focus_refuses_rather_than_raising_a_merely_similar_window():
    """No window for this session: focusing something adjacent would let the caller
    type a slash command into another project's agent. Refuse instead."""
    only_uploader = "0x0240000b  0 host Brainstorming session - game_uploader - Visual Studio Code\n"
    calls = []
    assert _focus("/home/aaron/game", _fake_run(calls, only_uploader)) is False
    assert calls == [["wmctrl", "-l"]], "nothing may be activated on a miss"


def test_focus_reports_failure_when_wmctrl_cannot_raise_the_window():
    """The window can close between listing and raising. Saying "ok" there lets the
    caller type a slash command into whatever happened to hold focus."""
    calls = []
    assert _focus("/home/aaron/game", _fake_run(calls, PREFIX_COLLISION, returncode=1)) is False


def test_focus_reports_failure_when_the_wm_refuses_to_raise_the_window():
    """wmctrl exits 0 even when focus-stealing prevention only flashes the taskbar."""
    calls = []
    run = _fake_run(calls, PREFIX_COLLISION, active="refuses")
    assert _focus("/home/aaron/game", run) is False
    assert _raised(calls) == ["0x024001b3"], "it tried, the window manager declined"


def test_focus_trusts_wmctrl_when_the_active_window_is_unreadable():
    """No xdotool is a reason to degrade, not to refuse every focus request."""
    calls = []
    assert _focus("/home/aaron/game", _fake_run(calls, PREFIX_COLLISION, active=None)) is True


def test_focus_matches_a_path_style_terminal_title():
    """Terminals title themselves with the path, not the bare folder name."""
    titles = "0x05000001  0 host aaron@host: ~/code/game\n"
    calls = []
    assert _focus("/home/aaron/code/game", _fake_run(calls, titles)) is True
    assert _raised(calls) == ["0x05000001"]
