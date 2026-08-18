import asyncio
import json

import pytest

from agent_monitor.daemon import Daemon
from agent_monitor.state import SessionRegistry


class FakePad:
    def __init__(self):
        self.shows = []

    async def run(self):
        await asyncio.Event().wait()

    async def show(self, colors, lines, names, flash, info, usage):
        self.shows.append((colors, lines, names, flash, info, usage))


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "state.json", tmp_path / "daemon.sock"


async def _send(sock_path, payload: dict):
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write(json.dumps(payload).encode() + b"\n")
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)


def _event(event="SessionStart", sid="a", pid=999):
    return {"event": event, "session_id": sid, "cwd": "/proj/x", "pid": pid}


async def _stop(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_event_updates_state_file_and_pad(paths):
    state_path, sock_path = paths
    pad = FakePad()
    daemon = Daemon(SessionRegistry(), pad, state_path, sock_path,
                    time_fn=lambda: 42.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event())
    state = json.loads(state_path.read_text())
    assert state["sessions"][0]["session_id"] == "a"
    assert state["sessions"][0]["status"] == "available"
    colors, lines, names, flash, info, usage = pad.shows[-1]
    assert colors[1] > 0  # slot 0 lights up green
    assert lines == []  # no usage_fn configured
    assert names[0] == "x"
    assert flash == [0] * 16
    assert info == [""] * 16
    assert usage == []
    await _stop(task)


async def test_invalid_lines_are_ignored(paths):
    state_path, sock_path = paths
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write(b"not json\n")
    writer.write(json.dumps({"event": "Stop"}).encode() + b"\n")  # no session_id
    await writer.drain()
    writer.close()
    await asyncio.sleep(0.05)
    assert json.loads(state_path.read_text())["sessions"] == []
    await _stop(task)


async def test_prune_loop_removes_dead_sessions(paths):
    state_path, sock_path = paths
    alive = {"value": True}
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: alive["value"],
                    prune_interval=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event())
    alive["value"] = False
    await asyncio.sleep(0.15)
    assert json.loads(state_path.read_text())["sessions"] == []
    await _stop(task)


async def test_state_loaded_on_start_and_pruned(paths):
    state_path, sock_path = paths
    state_path.write_text(json.dumps({"sessions": [
        {"session_id": "old-alive", "cwd": "/p", "pid": 1, "status": "busy",
         "slot": 5, "since": 1.0},
        {"session_id": "old-dead", "cwd": "/p", "pid": 2, "status": "busy",
         "slot": 1, "since": 1.0},
    ]}))
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 2.0, pid_alive=lambda pid: pid == 1)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    sessions = json.loads(state_path.read_text())["sessions"]
    assert [s["session_id"] for s in sessions] == ["old-alive"]
    assert sessions[0]["slot"] == 5
    await _stop(task)


async def test_stale_socket_file_is_replaced(paths):
    state_path, sock_path = paths
    sock_path.touch()
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event())
    assert json.loads(state_path.read_text())["sessions"]
    await _stop(task)


async def test_session_end_removes_session(paths):
    state_path, sock_path = paths
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event())
    await _send(sock_path, _event(event="SessionEnd"))
    assert json.loads(state_path.read_text())["sessions"] == []
    await _stop(task)


async def test_non_object_snapshot_is_ignored(paths):
    state_path, sock_path = paths
    state_path.write_text("null")
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    assert json.loads(state_path.read_text())["sessions"] == []
    await _stop(task)


async def test_oversized_line_does_not_kill_daemon(paths):
    state_path, sock_path = paths
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write(b"x" * 200_000 + b"\n")
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    await _send(sock_path, _event())  # a fresh connection must still work
    assert json.loads(state_path.read_text())["sessions"]
    await _stop(task)


async def test_second_daemon_refuses_to_steal_live_socket(paths):
    state_path, sock_path = paths
    d1 = Daemon(SessionRegistry(), None, state_path, sock_path,
                time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    t1 = asyncio.create_task(d1.run())
    await asyncio.wait_for(d1.ready.wait(), 2.0)
    d2 = Daemon(SessionRegistry(), None, state_path, sock_path,
                time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    with pytest.raises(RuntimeError):
        await d2.run()
    await _stop(t1)


async def test_rc_loop_updates_flags_quickly(paths):
    state_path, sock_path = paths
    rc = {"value": False}
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True,
                    rc_fn=lambda pid: rc["value"], rc_interval=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event())
    await asyncio.sleep(0.1)
    assert json.loads(state_path.read_text())["sessions"][0]["remote"] is False
    rc["value"] = True
    await asyncio.sleep(0.15)
    assert json.loads(state_path.read_text())["sessions"][0]["remote"] is True
    # A subsequent False from the detector is ignored (network hiccup latch)
    rc["value"] = False
    await asyncio.sleep(0.15)
    assert json.loads(state_path.read_text())["sessions"][0]["remote"] is True
    await _stop(task)


async def test_scan_loop_adds_unknown_sessions(paths):
    state_path, sock_path = paths
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True,
                    scan_fn=lambda: {4242: "/proj/scanned"}, scan_interval=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await asyncio.sleep(0.1)
    sessions = json.loads(state_path.read_text())["sessions"]
    assert sessions[0]["session_id"] == "proc-4242"
    assert sessions[0]["status"] == "unknown"
    await _stop(task)


async def test_ctx_loop_updates_context_and_usage(paths):
    from agent_monitor.context import ContextInfo, UsageLimit

    state_path, sock_path = paths
    pad = FakePad()
    daemon = Daemon(SessionRegistry(), pad, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True,
                    ctx_fn=lambda pid: ContextInfo(52, "fable-5", "xhigh"),
                    usage_fn=lambda: [UsageLimit("5h", 40, "13:40", False)],
                    ctx_interval=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event())
    await asyncio.sleep(0.1)
    sess = json.loads(state_path.read_text())["sessions"][0]
    assert (sess["model"], sess["effort"], sess["context_pct"]) == ("fable-5", "xhigh", 52)
    colors, lines, names, flash, info, usage = pad.shows[-1]
    assert lines == ["5h  40% -> 13:40"]
    assert usage == [40]
    assert info[0] == "fable-5 xhigh\nctx 52%"
    await _stop(task)


async def test_session_start_reclaims_dead_predecessors_key(paths):
    state_path, sock_path = paths
    registry = SessionRegistry()
    registry.add_scanned(999, "/proj/x", 1.0)   # old session on slot 0
    registry.add_scanned(500, "/proj/y", 1.0)   # neighbor on slot 1, stays alive
    daemon = Daemon(registry, None, state_path, sock_path,
                    time_fn=lambda: 2.0, pid_alive=lambda pid: pid != 999)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    # the reloaded session (new pid) starts before any prune tick ran
    await _send(sock_path, {"event": "SessionStart", "session_id": "reborn",
                            "cwd": "/proj/x", "pid": 1234})
    sessions = {s["session_id"]: s for s in json.loads(state_path.read_text())["sessions"]}
    assert "proc-999" not in sessions
    assert sessions["reborn"]["slot"] == 0
    assert sessions["proc-500"]["slot"] == 1
    await _stop(task)


async def test_run_action_option_mapping(paths, monkeypatch):
    from agent_monitor import actions
    from agent_monitor.model import Session, Status

    called = []
    monkeypatch.setattr(actions, "display_locked", lambda: False)
    monkeypatch.setattr(actions, "restart_session", lambda cwd: (called.append("restart"), True)[1])
    monkeypatch.setattr(actions, "toggle_remote_control", lambda cwd: (called.append("toggle"), True)[1])
    monkeypatch.setattr(actions, "compact_session", lambda cwd: (called.append("compact"), True)[1])
    state_path, sock_path = paths
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    sess = Session("a", "/proj/x", 1, Status.AVAILABLE, 0, 1.0)
    await daemon._run_action(sess, 0)
    await daemon._run_action(sess, 2)
    await daemon._run_action(sess, 7)  # unknown option — ignored
    assert called == ["restart", "compact"]


async def test_a_focus_that_finds_nothing_says_so_on_the_key(paths, monkeypatch):
    """A double-press on a session with no window must not look like a dead pad."""
    from agent_monitor import focus as focus_mod
    monkeypatch.setattr(focus_mod, "focus_window", lambda cwd: False)
    state_path, sock_path = paths
    pad = FakePad()
    daemon = Daemon(SessionRegistry(), pad, state_path, sock_path,
                    time_fn=lambda: 42.0, pid_alive=lambda pid: True,
                    note_seconds=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event())

    daemon.focus_slot(0)
    await asyncio.sleep(0.02)
    assert pad.shows[-1][4][0] == "no window"

    await asyncio.sleep(0.1)  # the note is transient — the overlay goes back to normal
    assert pad.shows[-1][4][0] == ""
    await _stop(task)


async def test_a_focus_that_lands_leaves_no_note(paths, monkeypatch):
    from agent_monitor import focus as focus_mod
    monkeypatch.setattr(focus_mod, "focus_window", lambda cwd: True)
    state_path, sock_path = paths
    pad = FakePad()
    daemon = Daemon(SessionRegistry(), pad, state_path, sock_path,
                    time_fn=lambda: 42.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event())

    daemon.focus_slot(0)
    await asyncio.sleep(0.02)
    assert pad.shows[-1][4] == [""] * 16
    await _stop(task)


async def test_action_feedback_notes_on_pad(paths, monkeypatch):
    from agent_monitor import actions

    state_path, sock_path = paths
    pad = FakePad()
    daemon = Daemon(SessionRegistry(), pad, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True,
                    note_seconds=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event())  # session on slot 0

    ran = []
    monkeypatch.setattr(actions, "display_locked", lambda: True)
    monkeypatch.setattr(actions, "compact_session", lambda cwd: ran.append(cwd) or True)
    daemon.action_slot(0, 2)
    await asyncio.sleep(0.02)
    assert ran == []  # locked: the action must not even be attempted
    assert any(s[4][0] == "locked" for s in pad.shows)

    monkeypatch.setattr(actions, "display_locked", lambda: False)
    daemon.action_slot(0, 2)
    await asyncio.sleep(0.02)
    assert ran == ["/proj/x"]
    assert any(s[4][0].startswith("compacting") for s in pad.shows)

    monkeypatch.setattr(actions, "compact_session", lambda cwd: False)
    daemon.action_slot(0, 2)
    await asyncio.sleep(0.02)
    assert any(s[4][0].startswith("failed") for s in pad.shows)
    await asyncio.sleep(0.1)  # notes expire again
    assert pad.shows[-1][4][0] == ""
    await _stop(task)


async def test_state_file_carries_usage(paths):
    from agent_monitor.context import UsageLimit

    state_path, sock_path = paths
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True,
                    usage_fn=lambda: [UsageLimit("5h", 37, "18:50", False)],
                    ctx_interval=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await asyncio.sleep(0.1)
    usage = json.loads(state_path.read_text())["usage"]
    assert usage == [{"label": "5h", "percent": 37, "resets_at": "18:50", "stale": False}]
    await _stop(task)


async def test_remote_loop_adds_and_drops_remote_sessions(paths):
    state_path, sock_path = paths
    probes = {"value": {"box": [{"session_id": "r1", "pid": 3024, "cwd": "/home/s/proj",
                                 "model": "opus-5", "effort": "high", "context_pct": 12,
                                 "age": 2.0}]}}
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1000.0, pid_alive=lambda pid: True,
                    remote_fn=lambda: probes["value"], remote_interval=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await asyncio.sleep(0.12)
    sessions = json.loads(state_path.read_text())["sessions"]
    assert sessions[0]["host"] == "box"
    assert sessions[0]["status"] == "busy"
    probes["value"] = {"box": []}          # window closed on the remote
    await asyncio.sleep(0.12)
    assert json.loads(state_path.read_text())["sessions"] == []
    await _stop(task)


async def test_unreachable_host_leaves_sessions_alone(paths):
    state_path, sock_path = paths
    probes = {"value": {"box": [{"session_id": "r1", "pid": 1, "cwd": "/p", "age": 2.0}]}}
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1000.0, pid_alive=lambda pid: True,
                    remote_fn=lambda: probes["value"], remote_interval=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await asyncio.sleep(0.12)
    probes["value"] = {}                   # host unreachable: no result at all
    await asyncio.sleep(0.12)
    assert len(json.loads(state_path.read_text())["sessions"]) == 1
    await _stop(task)


async def test_menu_hide_takes_the_key_and_reports_it(paths):
    state_path, sock_path = paths
    pad = FakePad()
    daemon = Daemon(SessionRegistry(), pad, state_path, sock_path,
                    time_fn=lambda: 100.0, pid_alive=lambda pid: True,
                    note_seconds=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event())            # session on slot 0
    daemon.action_slot(0, 3)
    await asyncio.sleep(0.05)
    sess = json.loads(state_path.read_text())["sessions"][0]
    assert sess["slot"] is None and sess["hidden_at"] == 100.0
    assert any(s[4][0] == "hidden" for s in pad.shows)
    await _stop(task)


async def test_menu_end_signals_and_reports(paths, monkeypatch):
    from agent_monitor import actions

    state_path, sock_path = paths
    pad = FakePad()
    ended = []
    monkeypatch.setattr(actions, "end_session", lambda pid: ended.append(pid) or True)
    daemon = Daemon(SessionRegistry(), pad, state_path, sock_path,
                    time_fn=lambda: 100.0, pid_alive=lambda pid: True,
                    note_seconds=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event(pid=4242))
    daemon.action_slot(0, 4)
    await asyncio.sleep(0.05)
    assert ended == [4242]
    assert any(s[4][0].startswith("ending") for s in pad.shows)
    await _stop(task)


async def test_menu_end_refuses_a_remote_session(paths, monkeypatch):
    from agent_monitor import actions

    state_path, sock_path = paths
    pad = FakePad()
    ended = []
    monkeypatch.setattr(actions, "end_session", lambda pid: ended.append(pid) or True)
    registry = SessionRegistry()
    registry.sync_remote("box", [{"session_id": "r1", "pid": 3024, "cwd": "/p",
                                  "age": 5.0}], now=100.0)
    daemon = Daemon(registry, pad, state_path, sock_path,
                    time_fn=lambda: 100.0, pid_alive=lambda pid: True,
                    note_seconds=0.05)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    daemon.action_slot(0, 4)
    await asyncio.sleep(0.05)
    assert ended == []
    # startswith, not ==: overlay_info (render.py) prepends the note to the
    # session's persistent overlay text, and a remote session always carries
    # an "@host" badge line — so the full overlay is "local only\n@box".
    assert any(s[4][0].startswith("local only") for s in pad.shows)
    await _stop(task)


async def test_setting_from_the_pad_flips_forward_mode_and_persists(paths):
    state_path, sock_path = paths
    pad = FakePad()
    registry = SessionRegistry()
    daemon = Daemon(registry, pad, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    daemon.set_setting("forward", True)
    await asyncio.sleep(0.05)
    assert registry.forward_mode is True
    assert json.loads(state_path.read_text())["forward_mode"] is True
    daemon.set_setting("forward", False)
    await asyncio.sleep(0.05)
    assert json.loads(state_path.read_text())["forward_mode"] is False
    await _stop(task)


async def test_an_unknown_setting_name_is_ignored(paths):
    state_path, sock_path = paths
    registry = SessionRegistry()
    daemon = Daemon(registry, None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    daemon.set_setting("teleport", True)
    await asyncio.sleep(0.05)
    assert registry.forward_mode is False
    await _stop(task)


async def test_forward_mode_survives_a_daemon_restart(paths):
    state_path, sock_path = paths
    state_path.write_text(json.dumps({"sessions": [], "forward_mode": True}))
    registry = SessionRegistry()
    daemon = Daemon(registry, None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    assert json.loads(state_path.read_text())["forward_mode"] is True
    await _stop(task)


async def test_hook_events_from_non_interactive_pid_are_dropped(paths, monkeypatch):
    """A `claude -p` one-shot or background agent still fires hooks, but must
    never show up on the deck — the user cannot interact with it (WB-165)."""
    from agent_monitor import scan
    state_path, sock_path = paths
    monkeypatch.setattr(scan, "is_interactive_pid",
                        lambda pid: pid != 4242)             # 4242 is background
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.ready.wait(), 2.0)
    await _send(sock_path, _event(sid="bg", pid=4242))       # dropped
    await _send(sock_path, _event(sid="fg", pid=999))        # kept
    sessions = json.loads(state_path.read_text())["sessions"]
    assert [s["session_id"] for s in sessions] == ["fg"]
    await _stop(task)
