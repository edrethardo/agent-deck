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


def test_same_status_and_finish_state_is_no_change():
    reg = SessionRegistry()
    _start(reg, "a", t=1.0)
    reg.apply_event("Stop", "a", "/proj/a", 100, None, 5.0)  # available -> finished
    assert reg.apply_event("Stop", "a", "/proj/a", 100, None, 9.0) is False
    assert reg.sessions()[0].since == 5.0


def test_stop_marks_session_finished_and_prompt_clears_it():
    reg = SessionRegistry()
    _start(reg, "a", t=1.0)
    reg.apply_event("UserPromptSubmit", "a", "/proj/a", 100, None, 2.0)
    assert reg.apply_event("Stop", "a", "/proj/a", 100, None, 3.0) is True
    assert (reg.by_id("a").status, reg.by_id("a").finished) == (Status.AVAILABLE, True)
    reg.apply_event("UserPromptSubmit", "a", "/proj/a", 100, None, 4.0)
    assert reg.by_id("a").finished is False


def test_finished_decays_after_hold():
    reg = SessionRegistry()
    _start(reg, "a", t=1.0)
    reg.apply_event("UserPromptSubmit", "a", "/proj/a", 100, None, 2.0)
    reg.apply_event("Stop", "a", "/proj/a", 100, None, 3.0)
    assert reg.decay_finished(100.0) is False  # within the 600s hold
    assert reg.decay_finished(700.0) is True
    assert reg.by_id("a").finished is False
    assert reg.decay_finished(800.0) is False


def test_restarted_session_reclaims_its_projects_key():
    reg = SessionRegistry()
    _start(reg, "a", t=1.0, pid=100)
    _start(reg, "b", t=1.0, pid=200)          # slot 1
    reg.swap_slots(1, 7)                       # user moved b to key 8
    reg.prune(lambda pid: pid != 200)          # b's process died (reload)
    reg.apply_event("SessionStart", "b2", "/proj/b", 300, None, 5.0)
    assert reg.by_id("b2").slot == 7           # reclaimed, despite slot 1 being free


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


def test_update_remote_flags_reports_changes():
    reg = SessionRegistry()
    _start(reg, "a", pid=100)
    assert reg.update_remote_flags(lambda pid: True) is True
    assert reg.by_id("a").remote is True
    assert reg.update_remote_flags(lambda pid: True) is False
    assert reg.update_remote_flags(lambda pid: False) is True


def test_scan_rediscovery_reclaims_projects_key():
    reg = SessionRegistry()
    _start(reg, "a", pid=100)              # slot 0
    _start(reg, "b", pid=200)              # slot 1
    reg.swap_slots(1, 6)                   # user moved b to key 7
    reg.prune(lambda pid: pid == 100)      # b's process died
    assert reg.add_scanned(300, "/proj/b", 5.0) is True
    assert reg.by_id("proc-300").slot == 6  # rediscovery lands on b's old key


def test_manual_rc_flag_pins_against_detection():
    reg = SessionRegistry()
    _start(reg, "a", pid=100)
    reg.update_remote_flags(lambda pid: True)
    assert reg.by_id("a").remote is True
    assert reg.set_remote_manual(0, False) is True   # pad toggled it off
    assert reg.by_id("a").remote is False
    reg.update_remote_flags(lambda pid: True)         # lingering relay socket
    assert reg.by_id("a").remote is False             # manual value wins


def test_update_context_sets_fields_and_reports_change():
    from agent_monitor.context import ContextInfo

    reg = SessionRegistry()
    _start(reg, "a", pid=5)
    assert reg.update_context({5: ContextInfo(42, "fable-5", "high")}) is True
    (s,) = reg.sessions()
    assert (s.context_pct, s.model, s.effort) == (42, "fable-5", "high")
    # same values again: no change
    assert reg.update_context({5: ContextInfo(42, "fable-5", "high")}) is False
    # unreadable transcript: keep the last known values
    assert reg.update_context({5: None}) is False
    assert reg.update_context({}) is False
    assert s.context_pct == 42


def test_update_context_adopts_question_flag():
    from agent_monitor.context import ContextInfo

    reg = SessionRegistry()
    _start(reg, "a", pid=5)
    assert reg.update_context({5: ContextInfo(10, "fable-5", "high", question=True)}) is True
    assert reg.sessions()[0].question is True
    assert reg.update_context({5: ContextInfo(10, "fable-5", "high", question=False)}) is True
    assert reg.sessions()[0].question is False


def test_post_stop_activity_flips_available_back_to_busy():
    from agent_monitor.context import ContextInfo
    from agent_monitor.model import Status

    reg = SessionRegistry()
    _start(reg, "a", pid=5)
    reg.apply_event("Stop", "a", "/proj/a", 5, None, 100.0)  # finished -> green
    (s,) = reg.sessions()
    assert (s.status, s.finished) == (Status.AVAILABLE, True)
    # transcript written AFTER the Stop and recently: autonomous continuation
    info = ContextInfo(10, "fable-5", "high", activity=110.0)
    assert reg.update_context({5: info}, now=115.0) is True
    assert (s.status, s.finished, s.since) == (Status.BUSY, False, 110.0)


def test_stale_or_pre_stop_activity_does_not_flip():
    from agent_monitor.context import ContextInfo
    from agent_monitor.model import Status

    reg = SessionRegistry()
    _start(reg, "a", pid=5)
    reg.apply_event("Stop", "a", "/proj/a", 5, None, 100.0)
    (s,) = reg.sessions()
    # activity BEFORE the Stop: the turn that just ended — stays green
    reg.update_context({5: ContextInfo(10, "fable-5", "high", activity=99.0)}, now=105.0)
    assert s.status is Status.AVAILABLE
    # activity after the Stop but long ago (daemon restart case) — stays green
    reg.update_context({5: ContextInfo(10, "fable-5", "high", activity=110.0)}, now=500.0)
    assert s.status is Status.AVAILABLE


def test_activity_never_downgrades_waiting():
    from agent_monitor.context import ContextInfo
    from agent_monitor.model import Status

    reg = SessionRegistry()
    _start(reg, "a", pid=5)
    reg.apply_event("PermissionRequest", "a", "/proj/a", 5, None, 100.0)
    (s,) = reg.sessions()
    reg.update_context({5: ContextInfo(10, "fable-5", "high", activity=110.0)}, now=112.0)
    assert s.status is Status.WAITING


def test_cwd_is_frozen_at_creation():
    reg = SessionRegistry()
    _start(reg, "a")  # cwd /proj/a
    # the session cd's into a subdirectory — hook events report the new cwd
    reg.apply_event("UserPromptSubmit", "a", "/proj/a/docs/plans", 100, None, 2.0)
    (s,) = reg.sessions()
    assert s.cwd == "/proj/a"  # key identity and name must not drift


def _ctx(**kw):
    from agent_monitor.context import ContextInfo
    base = dict(percent=10, model="fable-5", effort="high")
    base.update(kw)
    return ContextInfo(**base)


def test_blocked_flag_is_adopted():
    reg = SessionRegistry()
    _start(reg, "a", pid=5)
    assert reg.update_context({5: _ctx(blocked=True)}, now=10.0) is True
    assert reg.sessions()[0].blocked is True
    assert reg.update_context({5: _ctx(blocked=False)}, now=11.0) is True
    assert reg.sessions()[0].blocked is False


def test_waiting_for_peer_set_while_the_peer_is_busy():
    reg = SessionRegistry()
    _start(reg, "sender", t=1.0, pid=5)
    _start(reg, "peer", t=1.0, pid=9)
    reg.apply_event("Stop", "sender", "/proj/sender", 5, None, 2.0)   # green
    reg.apply_event("UserPromptSubmit", "peer", "/proj/peer", 9, None, 3.0)  # peer works
    reg.update_context({5: _ctx(peer_name="peer-9e", peer_pid=9), 9: _ctx()}, now=10.0)
    sender = [s for s in reg.sessions() if s.session_id == "sender"][0]
    assert sender.waiting_for == "peer-9e"


def test_waiting_for_a_blocked_peer_also_counts():
    reg = SessionRegistry()
    _start(reg, "sender", pid=5)
    _start(reg, "peer", pid=9)
    reg.update_context({5: _ctx(peer_name="p", peer_pid=9), 9: _ctx(blocked=True)}, now=10.0)
    sender = [s for s in reg.sessions() if s.session_id == "sender"][0]
    assert sender.waiting_for == "p"


def test_waiting_for_cleared_when_the_peer_goes_idle():
    reg = SessionRegistry()
    _start(reg, "sender", pid=5)
    _start(reg, "peer", pid=9)
    reg.apply_event("UserPromptSubmit", "peer", "/proj/peer", 9, None, 3.0)
    reg.update_context({5: _ctx(peer_name="p", peer_pid=9), 9: _ctx()}, now=10.0)
    reg.apply_event("Stop", "peer", "/proj/peer", 9, None, 4.0)  # peer answered
    reg.update_context({5: _ctx(peer_name="p", peer_pid=9), 9: _ctx()}, now=11.0)
    sender = [s for s in reg.sessions() if s.session_id == "sender"][0]
    assert sender.waiting_for == ""


def test_unknown_peer_pid_does_not_set_waiting():
    reg = SessionRegistry()
    _start(reg, "sender", pid=5)
    reg.update_context({5: _ctx(peer_name="ghost", peer_pid=999)}, now=10.0)
    assert reg.sessions()[0].waiting_for == ""
