from agent_monitor import scan


def test_looks_like_claude():
    assert scan.looks_like_claude(["claude"])
    assert scan.looks_like_claude(["/usr/bin/node", "/home/x/.vscode/extensions/anthropic.claude-code-2.0/cli.js"])
    assert not scan.looks_like_claude(["bash", "-c"])
    assert not scan.looks_like_claude([])


def test_looks_like_claude_real_shapes():
    assert scan.looks_like_claude(
        ["/home/a/.vscode/extensions/anthropic.claude-code-2.1/resources/native-binary/claude",
         "--output-format", "stream-json"])
    assert scan.looks_like_claude(["/usr/bin/node", "/home/x/.npm/claude-code/cli.js"])
    assert not scan.looks_like_claude(["grep", "claude", "x"])
    assert not scan.looks_like_claude(["cat", "/home/a/.claude/settings.json"])
    assert not scan.looks_like_claude(["python3", "/tmp/claude-1000/x/y.py"])
    assert not scan.looks_like_claude(["nvim", "/home/a/.claude/settings.json"])


def test_ancestor_walk_finds_claude(monkeypatch):
    tree = {10: (["/bin/sh", "-c"], 5), 5: (["node", "/ext/claude-code/cli.js"], 2), 2: (["init"], 1)}
    monkeypatch.setattr(scan, "_cmdline", lambda p: tree.get(p, ([], 0))[0])
    monkeypatch.setattr(scan, "_ppid", lambda p: tree.get(p, ([], 0))[1])
    assert scan.claude_ancestor_pid(10) == 5


def test_ancestor_walk_falls_back_to_start(monkeypatch):
    monkeypatch.setattr(scan, "_cmdline", lambda p: ["bash"])
    monkeypatch.setattr(scan, "_ppid", lambda p: 1)
    assert scan.claude_ancestor_pid(42) == 42


def test_scan_keeps_topmost_match(monkeypatch):
    cmds = {100: ["/opt/claude/claude"], 101: ["/opt/claude/claude", "-p"], 200: ["bash"]}
    parents = {100: 1, 101: 100, 200: 1}
    monkeypatch.setattr(scan, "_iter_pids", lambda: [100, 101, 200])
    monkeypatch.setattr(scan, "_same_uid", lambda p: True)
    monkeypatch.setattr(scan, "_cmdline", lambda p: cmds.get(p, []))
    monkeypatch.setattr(scan, "_ppid", lambda p: parents.get(p, 0))
    monkeypatch.setattr(scan, "_cwd", lambda p: f"/proj/{p}")
    assert scan.claude_processes() == {100: "/proj/100"}
