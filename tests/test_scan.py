from agent_monitor import scan


def test_looks_like_claude():
    assert scan.looks_like_claude(["claude"])
    assert scan.looks_like_claude(["/usr/bin/node", "/home/x/.vscode/extensions/anthropic.claude-code-2.0/cli.js"])
    assert not scan.looks_like_claude(["bash", "-c"])
    assert not scan.looks_like_claude([])


def test_ancestor_walk_finds_claude(monkeypatch):
    tree = {10: (["/bin/sh", "-c"], 5), 5: (["node", "/ext/claude-code/cli.js"], 2), 2: (["init"], 1)}
    monkeypatch.setattr(scan, "_cmdline", lambda p: tree.get(p, ([], 0))[0])
    monkeypatch.setattr(scan, "_ppid", lambda p: tree.get(p, ([], 0))[1])
    assert scan.claude_ancestor_pid(10) == 5


def test_ancestor_walk_falls_back_to_start(monkeypatch):
    monkeypatch.setattr(scan, "_cmdline", lambda p: ["bash"])
    monkeypatch.setattr(scan, "_ppid", lambda p: 1)
    assert scan.claude_ancestor_pid(42) == 42


def test_scan_keeps_leaf_matches(monkeypatch):
    cmds = {100: ["code/extensions/claude-code"], 101: ["node", "claude"], 102: ["node", "claude"], 200: ["bash"]}
    parents = {100: 1, 101: 100, 102: 100, 200: 1}
    monkeypatch.setattr(scan, "_iter_pids", lambda: [100, 101, 102, 200])
    monkeypatch.setattr(scan, "_same_uid", lambda p: True)
    monkeypatch.setattr(scan, "_cmdline", lambda p: cmds.get(p, []))
    monkeypatch.setattr(scan, "_ppid", lambda p: parents.get(p, 0))
    monkeypatch.setattr(scan, "_cwd", lambda p: f"/proj/{p}")
    assert scan.claude_processes() == {101: "/proj/101", 102: "/proj/102"}
