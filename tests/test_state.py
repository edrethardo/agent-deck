from agent_monitor.model import Status
from agent_monitor.state import SessionRegistry


def _start(reg, sid, t=1.0, pid=100):
    return reg.apply_event("SessionStart", sid, f"/proj/{sid}", pid, None, t)


def test_new_session_gets_slot_zero_and_available():
    reg = SessionRegistry()
    assert _start(reg, "a") is True
    (s,) = reg.sessions()
    assert (s.slot, s.status) == (0, Status.AVAILABLE)


def test_second_session_gets_next_slot():
    reg = SessionRegistry()
    _start(reg, "a")
    _start(reg, "b")
    assert [s.slot for s in reg.sessions()] == [0, 1]


def test_status_change_updates_since_and_reports_change():
    reg = SessionRegistry()
    _start(reg, "a", t=1.0)
    assert reg.apply_event("UserPromptSubmit", "a", "/proj/a", 100, None, 5.0) is True
    (s,) = reg.sessions()
    assert (s.status, s.since) == (Status.BUSY, 5.0)


def test_same_status_is_no_change():
    reg = SessionRegistry()
    _start(reg, "a", t=1.0)
    assert reg.apply_event("Stop", "a", "/proj/a", 100, None, 9.0) is False
    assert reg.sessions()[0].since == 1.0


def test_event_without_status_change_is_no_change():
    reg = SessionRegistry()
    _start(reg, "a")
    assert reg.apply_event("PreCompact", "a", "/proj/a", 100, None, 2.0) is False


def test_unknown_session_created_with_event_status():
    reg = SessionRegistry()
    msg = "Claude needs your permission to use Bash"
    reg.apply_event("Notification", "x", "/proj/x", 7, msg, 1.0)
    assert reg.sessions()[0].status == Status.WAITING


def test_session_end_frees_slot_for_reuse():
    reg = SessionRegistry()
    _start(reg, "a")
    _start(reg, "b")
    assert reg.apply_event("SessionEnd", "a", "/proj/a", 100, None, 2.0) is True
    _start(reg, "c")
    assert sorted(s.slot for s in reg.sessions()) == [0, 1]


def test_seventeenth_session_overflows_then_gets_freed_slot():
    reg = SessionRegistry()
    for i in range(16):
        _start(reg, f"s{i}")
    _start(reg, "overflow")
    assert reg.by_id("overflow").slot is None
    reg.apply_event("SessionEnd", "s3", "/proj/s3", 100, None, 2.0)
    assert reg.by_id("overflow").slot == 3


def test_prune_removes_dead_sessions():
    reg = SessionRegistry()
    _start(reg, "a", pid=111)
    _start(reg, "b", pid=222)
    assert reg.prune(lambda pid: pid == 222) is True
    assert [s.session_id for s in reg.sessions()] == ["b"]
    assert reg.prune(lambda pid: True) is False


def test_roundtrip_serialization():
    reg = SessionRegistry()
    _start(reg, "a")
    reg2 = SessionRegistry.from_dict(reg.to_dict())
    assert reg2.sessions()[0].session_id == "a"


def test_from_dict_tolerates_garbage():
    assert SessionRegistry.from_dict({"sessions": [{"nope": 1}]}).sessions() == []
    assert SessionRegistry.from_dict({}).sessions() == []


def test_from_dict_normalizes_corrupt_slots():
    data = {"sessions": [
        {"session_id": "a", "cwd": "/p", "pid": 1, "status": "busy", "slot": 0, "since": 1.0},
        {"session_id": "b", "cwd": "/p", "pid": 2, "status": "busy", "slot": 0, "since": 2.0},
        {"session_id": "c", "cwd": "/p", "pid": 3, "status": "busy", "slot": 99, "since": 3.0},
        {"session_id": "d", "cwd": "/p", "pid": 4, "status": "busy", "slot": "5", "since": 4.0},
    ]}
    reg = SessionRegistry.from_dict(data)
    slots = {s.session_id: s.slot for s in reg.sessions()}
    assert slots == {"b": 0, "a": 1, "c": 2, "d": 3}


def test_from_dict_promotes_overflow_when_slots_free():
    data = {"sessions": [
        {"session_id": "a", "cwd": "/p", "pid": 1, "status": "busy", "slot": None, "since": 1.0},
    ]}
    reg = SessionRegistry.from_dict(data)
    assert reg.sessions()[0].slot == 0


def test_promote_overflow_orders_by_since():
    reg = SessionRegistry()
    for i in range(16):
        _start(reg, f"s{i}", t=float(i))
    _start(reg, "late", t=100.0)
    _start(reg, "early", t=50.0)
    reg.apply_event("SessionEnd", "s0", "/proj/s0", 100, None, 200.0)
    reg.apply_event("SessionEnd", "s1", "/proj/s1", 100, None, 200.0)
    slots = {s.session_id: s.slot for s in reg.sessions()}
    assert slots["early"] == 0
    assert slots["late"] == 1


def test_add_scanned_creates_unknown_session():
    reg = SessionRegistry()
    assert reg.add_scanned(777, "/proj/scanned", 1.0) is True
    (s,) = reg.sessions()
    assert (s.session_id, s.status, s.slot, s.pid) == ("proc-777", Status.UNKNOWN, 0, 777)


def test_add_scanned_skips_known_pid():
    reg = SessionRegistry()
    _start(reg, "real", pid=777)
    assert reg.add_scanned(777, "/proj/x", 2.0) is False
    assert reg.add_scanned(777, "/proj/x", 2.0) is False


def test_real_event_takes_over_scanned_slot():
    reg = SessionRegistry()
    reg.add_scanned(777, "/proj/x", 1.0)
    reg.add_scanned(888, "/proj/y", 1.0)
    reg.apply_event("SessionStart", "real-id", "/proj/x", 777, None, 2.0)
    assert reg.by_id("proc-777") is None
    assert reg.by_id("real-id").slot == 0
    assert reg.by_id("proc-888").slot == 1


def test_low_pid_event_does_not_overwrite_good_pid():
    reg = SessionRegistry()
    _start(reg, "a", pid=4321)
    reg.apply_event("UserPromptSubmit", "a", "/proj/a", 1, None, 2.0)
    assert reg.by_id("a").pid == 4321


def test_takeover_from_overflow_twin_claims_free_slot():
    reg = SessionRegistry()
    reg.add_scanned(777, "/p/x", 1.0)
    reg.by_id("proc-777").slot = None  # simulate an overflow twin
    reg.apply_event("SessionStart", "real", "/p/x", 777, None, 2.0)
    assert reg.by_id("real").slot == 0


def test_real_session_evicts_newest_scanned_when_full():
    reg = SessionRegistry()
    for i in range(16):
        reg.add_scanned(1000 + i, f"/p/{i}", float(i))
    reg.apply_event("SessionStart", "real", "/p/real", 5, None, 99.0)
    assert reg.by_id("real").slot == 15
    assert reg.by_id("proc-1015").slot is None


def test_swap_slots_between_occupied_keys():
    reg = SessionRegistry()
    _start(reg, "a")
    _start(reg, "b")
    assert reg.swap_slots(0, 1) is True
    assert (reg.by_id("a").slot, reg.by_id("b").slot) == (1, 0)


def test_swap_slot_to_empty_key():
    reg = SessionRegistry()
    _start(reg, "a")
    assert reg.swap_slots(0, 7) is True
    assert reg.by_id("a").slot == 7


def test_swap_empty_slots_is_no_change():
    reg = SessionRegistry()
    assert reg.swap_slots(3, 4) is False
    assert reg.swap_slots(3, 3) is False
