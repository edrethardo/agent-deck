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


V6_HEADER = "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
V6_RELAY = "C06B072600000000000000001000000000000000"[:32]  # 2607:6bc0::10


def test_established_v6_words_parse_relay_connection(tmp_path):
    content = (
        V6_HEADER
        + f"   0: 00000000000000000000000000000000:1F90 {V6_RELAY}:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 111\n"
    )
    p = tmp_path / "tcp6"
    p.write_text(content)
    assert scan._established_v6_first_words({"111"}, tcp6_path=str(p)) == ["c06b0726"]


def test_established_v6_words_ignore_non_established_and_other_inodes(tmp_path):
    content = (
        V6_HEADER
        + f"   0: 00000000000000000000000000000000:1F90 {V6_RELAY}:01BB 06 00000000:00000000 00:00000000 00000000  1000        0 111\n"
        + f"   1: 00000000000000000000000000000000:1F90 {V6_RELAY}:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 222\n"
    )
    p = tmp_path / "tcp6"
    p.write_text(content)
    assert scan._established_v6_first_words({"111"}, tcp6_path=str(p)) == []


def test_has_remote_control_false_for_dead_pid():
    assert scan.has_remote_control(999999999) is False


def test_established_v4_remotes_parse(tmp_path):
    content = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:1F90 0A684FA0:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 111\n"
    )
    p = tmp_path / "tcp"
    p.write_text(content)
    # 0A684FA0 little-endian -> 160.79.104.10
    assert scan._established_v4_remotes({"111"}, tcp_path=str(p)) == ["160.79.104.10"]
    assert scan._established_v4_remotes({"999"}, tcp_path=str(p)) == []
