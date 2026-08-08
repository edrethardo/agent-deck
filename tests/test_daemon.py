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

    async def show(self, colors, lines):
        self.shows.append((colors, lines))


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


async def test_event_updates_state_file_and_pad(paths):
    state_path, sock_path = paths
    pad = FakePad()
    daemon = Daemon(SessionRegistry(), pad, state_path, sock_path,
                    time_fn=lambda: 42.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await daemon.ready.wait()
    await _send(sock_path, _event())
    state = json.loads(state_path.read_text())
    assert state["sessions"][0]["session_id"] == "a"
    assert state["sessions"][0]["status"] == "available"
    colors, lines = pad.shows[-1]
    assert colors[1] > 0  # slot 0 lights up green
    assert lines == [" 1 x            +"]
    task.cancel()


async def test_invalid_lines_are_ignored(paths):
    state_path, sock_path = paths
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await daemon.ready.wait()
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write(b"not json\n")
    writer.write(json.dumps({"event": "Stop"}).encode() + b"\n")  # no session_id
    await writer.drain()
    writer.close()
    await asyncio.sleep(0.05)
    assert json.loads(state_path.read_text())["sessions"] == []
    task.cancel()


async def test_prune_loop_removes_dead_sessions(paths):
    state_path, sock_path = paths
    alive = {"value": True}
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: alive["value"],
                    prune_interval=0.05)
    task = asyncio.create_task(daemon.run())
    await daemon.ready.wait()
    await _send(sock_path, _event())
    alive["value"] = False
    await asyncio.sleep(0.15)
    assert json.loads(state_path.read_text())["sessions"] == []
    task.cancel()


async def test_state_loaded_on_start_and_pruned(paths):
    state_path, sock_path = paths
    state_path.write_text(json.dumps({"sessions": [
        {"session_id": "old-alive", "cwd": "/p", "pid": 1, "status": "busy",
         "slot": 0, "since": 1.0},
        {"session_id": "old-dead", "cwd": "/p", "pid": 2, "status": "busy",
         "slot": 1, "since": 1.0},
    ]}))
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 2.0, pid_alive=lambda pid: pid == 1)
    task = asyncio.create_task(daemon.run())
    await daemon.ready.wait()
    sessions = json.loads(state_path.read_text())["sessions"]
    assert [s["session_id"] for s in sessions] == ["old-alive"]
    task.cancel()


async def test_stale_socket_file_is_replaced(paths):
    state_path, sock_path = paths
    sock_path.touch()
    daemon = Daemon(SessionRegistry(), None, state_path, sock_path,
                    time_fn=lambda: 1.0, pid_alive=lambda pid: True)
    task = asyncio.create_task(daemon.run())
    await daemon.ready.wait()
    await _send(sock_path, _event())
    assert json.loads(state_path.read_text())["sessions"]
    task.cancel()
