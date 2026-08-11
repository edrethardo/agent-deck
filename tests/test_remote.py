import json
import subprocess
from types import SimpleNamespace

from agent_monitor import remote

PS_OUTPUT = """\
/snap/code/254/usr/share/code/code /home/a/.vscode/extensions/ms-vscode-remote.remote-ssh/out/localServer.js
ssh -v -T -D 37251 -o ConnectTimeout=15 spheron-AI-PC
ssh -v -T -D 37843 -o ConnectTimeout=15 spheron-AI-PC
ssh -o BatchMode=yes other-host python3 -
sshd: aaron@pts/3
"""


def _run(stdout="", returncode=0, stderr="", capture=None):
    def run(cmd, **kwargs):
        if capture is not None:
            capture.append((cmd, kwargs))
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    return run


def test_discover_hosts_finds_vscode_tunnels_once(monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda n: "/usr/bin/ssh")
    assert remote.discover_hosts(run=_run(PS_OUTPUT)) == ["spheron-AI-PC"]


def test_discover_hosts_without_ssh_installed(monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda n: None)
    assert remote.discover_hosts(run=_run(PS_OUTPUT)) == []


def test_probe_script_is_self_contained_and_runs_locally(tmp_path):
    # the probe is context.py + a main; it must execute on a bare python3
    script = remote.probe_script()
    assert "_probe_main()" in script
    assert "import" in script
    res = subprocess.run(["python3", "-c", script], capture_output=True, text=True, timeout=30,
                         env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"sessions": []}  # empty fake HOME


def test_probe_host_parses_sessions():
    payload = json.dumps({"sessions": [{"pid": 3024, "cwd": "/home/spheron/p", "age": 5.0}]})
    assert remote.probe_host("h", run=_run(payload), script="x") == [
        {"pid": 3024, "cwd": "/home/spheron/p", "age": 5.0}]


def test_probe_host_failures_return_none():
    assert remote.probe_host("h", run=_run("", returncode=255, stderr="denied"), script="x") is None
    assert remote.probe_host("h", run=_run("not json"), script="x") is None
    assert remote.probe_host("h", run=_run(json.dumps({"nope": 1})), script="x") is None

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 20)
    assert remote.probe_host("h", run=boom, script="x") is None


def test_probe_host_uses_batchmode_and_pipes_the_script():
    calls = []
    remote.probe_host("myhost", run=_run(json.dumps({"sessions": []}), capture=calls), script="SRC")
    cmd, kwargs = calls[0]
    assert cmd[0] == "ssh" and cmd[-3:] == ["myhost", "python3", "-"]
    assert "BatchMode=yes" in cmd
    assert kwargs["input"] == "SRC"


def test_probe_all_skips_unreachable_hosts(monkeypatch):
    def run(cmd, **kwargs):
        if cmd[0] == "ps":
            return SimpleNamespace(stdout=PS_OUTPUT, stderr="", returncode=0)
        if "bad" in cmd:
            return SimpleNamespace(stdout="", stderr="x", returncode=255)
        return SimpleNamespace(stdout=json.dumps({"sessions": [{"pid": 1}]}), stderr="",
                               returncode=0)
    assert remote.probe_all(hosts=["good", "bad"], run=run) == {"good": [{"pid": 1}]}
