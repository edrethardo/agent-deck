from agent_monitor.context import UsageLimit
from agent_monitor.model import Session, Status
from agent_monitor.render import (
    BRIGHTNESS,
    NUM_KEY_LEDS,
    flash_flags,
    key_names,
    led_colors,
    overlay_info,
    usage_lines,
    usage_percents,
)


def _sess(slot, status=Status.AVAILABLE, cwd="/home/aaron/code/myproj", sid="a"):
    return Session(sid, cwd, 1, status, slot, 0.0)


def test_empty_registry_is_all_dark():
    assert led_colors([]) == [0] * (NUM_KEY_LEDS * 3)


def test_idle_local_session_lights_white():
    colors = led_colors([_sess(0)])
    w = int(255 * BRIGHTNESS)
    assert colors[0:3] == [w, w, w]
    assert colors[3:] == [0] * (NUM_KEY_LEDS * 3 - 3)


def test_idle_remote_session_lights_blue():
    sess = _sess(0)
    sess.remote = True
    colors = led_colors([sess])
    assert colors[0:3] == [0, 0, int(255 * BRIGHTNESS)]


def test_waiting_session_lights_red_on_its_slot():
    colors = led_colors([_sess(5, Status.WAITING)])
    assert colors[15:18] == [int(255 * BRIGHTNESS), 0, 0]


def test_overflow_session_not_rendered():
    assert led_colors([_sess(None)]) == [0] * (NUM_KEY_LEDS * 3)


def test_busy_session_lights_orange():
    colors = led_colors([_sess(1, Status.BUSY)])
    assert colors[3:6] == [int(255 * BRIGHTNESS), int(160 * BRIGHTNESS), 0]


def test_out_of_range_slot_is_skipped():
    assert led_colors([_sess(16)]) == [0] * (NUM_KEY_LEDS * 3)
    assert led_colors([_sess(-1)]) == [0] * (NUM_KEY_LEDS * 3)


def test_unknown_session_lights_white_when_not_remote():
    colors = led_colors([_sess(0, Status.UNKNOWN)])
    w = int(255 * BRIGHTNESS)
    assert colors[0:3] == [w, w, w]


def test_flash_flags_are_all_zero():
    sessions = [_sess(0, Status.UNKNOWN), _sess(1, Status.BUSY)]
    flags = flash_flags(sessions)
    assert flags == [0] * 16


def test_key_names_maps_full_names_by_slot():
    sessions = [
        _sess(0, cwd="/home/aaron/code/abcdefghijklmnopqrstuvwxyz123"),
        _sess(3, sid="b", cwd="/x/short"),
        _sess(None, sid="c"),
    ]
    names = key_names(sessions)
    assert len(names) == 16
    assert names[0] == "abcdefghijklmnopqrstuvwxy"  # truncated to 25
    assert names[3] == "short"
    assert names[1] == ""


def test_remote_session_name_gets_marker():
    sess = _sess(0, cwd="/home/aaron/code/abcdefghijklmnopqrstuvwxyz123")
    sess.remote = True
    names = key_names([sess])
    assert names[0].endswith(" [R]")
    assert len(names[0]) <= 25


def test_finished_session_lights_green():
    sess = _sess(0)
    sess.finished = True
    colors = led_colors([sess])
    assert colors[0:3] == [0, int(255 * BRIGHTNESS), 0]


def test_usage_lines_and_percents():
    limits = [
        UsageLimit("5h", 100, "13:40", False),
        UsageLimit("7d", 71, "Tue 16:00", False),
    ]
    assert usage_lines(limits) == ["5h 100% -> 13:40", "7d  71% -> Tue 16:00"]
    assert usage_percents(limits) == [100, 71]


def test_usage_stale_limit_shows_placeholder_without_bar():
    limits = [UsageLimit("5h", 100, "13:40", True)]
    assert usage_lines(limits) == ["5h  --%"]
    assert usage_percents(limits) == [-1]


def test_usage_without_reset_time_has_no_arrow():
    assert usage_lines([UsageLimit("7d", 5, "", False)]) == ["7d   5%"]


def test_usage_empty():
    assert usage_lines([]) == []
    assert usage_percents([]) == []


def test_overlay_info_per_slot():
    a = _sess(0)
    a.model, a.effort, a.context_pct = "fable-5", "xhigh", 52
    b = _sess(3, sid="b")
    b.model = "opus-5"  # context not known (yet)
    c = _sess(5, sid="c")  # no data at all
    info = overlay_info([a, b, c, _sess(None, sid="d")])
    assert len(info) == 16
    assert info[0] == "fable-5 xhigh\nctx 52%"
    assert info[3] == "opus-5"
    assert info[5] == ""


def test_overlay_note_leads_and_keeps_the_session_detail():
    a = _sess(0)
    a.model, a.effort, a.context_pct = "fable-5", "xhigh", 52
    info = overlay_info([a], {0: "no window"})
    assert info[0] == "no window\nfable-5 xhigh\nctx 52%"


def test_overlay_note_on_a_slot_with_no_detail_yet():
    info = overlay_info([_sess(2)], {2: "no window"})
    assert info[2] == "no window", "no trailing blank line to waste an OLED row"


def test_overlay_ignores_notes_off_the_board():
    info = overlay_info([_sess(0)], {99: "nope", -1: "nope", 0: ""})
    assert info == [""] * 16


def test_overlay_marks_high_context():
    a = _sess(0)
    a.model, a.effort, a.context_pct = "opus-5", "xhigh", 91
    assert overlay_info([a])[0] == "opus-5 xhigh\nctx 91% !"


def test_pending_question_lights_red():
    sess = _sess(0, Status.BUSY)
    sess.question = True
    assert led_colors([sess])[0:3] == [int(255 * BRIGHTNESS), 0, 0]


def test_blocked_session_lights_red():
    sess = _sess(0)
    sess.blocked = True
    assert led_colors([sess])[0:3] == [int(255 * BRIGHTNESS), 0, 0]


def test_session_waiting_for_a_peer_is_yellow_not_green():
    sess = _sess(0)
    sess.finished = True          # would be green
    sess.waiting_for = "peer-9e"
    assert led_colors([sess])[0:3] == [int(255 * BRIGHTNESS), int(160 * BRIGHTNESS), 0]


def test_overlay_explains_blocked_and_peer_wait():
    a = _sess(0)
    a.model, a.context_pct, a.blocked = "fable-5", 40, True
    b = _sess(1, sid="b")
    b.waiting_for = "unternehmen-9e"
    info = overlay_info([a, b])
    assert info[0].startswith("LIMIT REACHED")
    assert info[1].startswith("waits: unternehmen-9e")
