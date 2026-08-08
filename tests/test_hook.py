import io
import json
import socket

from agent_monitor import hook, paths


def _fake_stdin(monkeypatch, payload: str):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))


def _listen(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(paths.socket_path()))
    srv.listen(1)
    srv.settimeout(2)
    return srv


def test_hook_forwards_event(monkeypatch, tmp_path):
    srv = _listen(monkeypatch, tmp_path)
    _fake_stdin(monkeypatch, json.dumps({
        "hook_event_name": "Stop",
        "session_id": "abc",
        "cwd": "/proj",
    }))
    assert hook.main() == 0
    conn, _ = srv.accept()
    data = json.loads(conn.recv(4096).decode())
    assert data["event"] == "Stop"
    assert data["session_id"] == "abc"
    assert data["cwd"] == "/proj"
    assert isinstance(data["pid"], int) and data["pid"] > 0
    conn.close()
    srv.close()


def test_hook_forwards_notification_message(monkeypatch, tmp_path):
    srv = _listen(monkeypatch, tmp_path)
    _fake_stdin(monkeypatch, json.dumps({
        "hook_event_name": "Notification",
        "session_id": "abc",
        "cwd": "/proj",
        "message": "Claude needs your permission to use Bash",
    }))
    assert hook.main() == 0
    conn, _ = srv.accept()
    assert "permission" in json.loads(conn.recv(4096).decode())["message"]
    conn.close()
    srv.close()


def test_hook_without_daemon_still_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _fake_stdin(monkeypatch, json.dumps({"hook_event_name": "Stop", "session_id": "x"}))
    assert hook.main() == 0


def test_hook_with_garbage_stdin_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _fake_stdin(monkeypatch, "not json")
    assert hook.main() == 0
