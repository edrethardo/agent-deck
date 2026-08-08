from agent_monitor.model import Session, Status
from agent_monitor.render import BRIGHTNESS, NUM_KEY_LEDS, flash_flags, key_names, led_colors, oled_lines


def _sess(slot, status=Status.AVAILABLE, cwd="/home/aaron/code/myproj", sid="a"):
    return Session(sid, cwd, 1, status, slot, 0.0)


def test_empty_registry_is_all_dark():
    assert led_colors([]) == [0] * (NUM_KEY_LEDS * 3)
    assert oled_lines([]) == []


def test_available_session_lights_green():
    colors = led_colors([_sess(0)])
    assert colors[0:3] == [0, int(255 * BRIGHTNESS), 0]
    assert colors[3:] == [0] * (NUM_KEY_LEDS * 3 - 3)


def test_waiting_session_lights_red_on_its_slot():
    colors = led_colors([_sess(5, Status.WAITING)])
    assert colors[15:18] == [int(255 * BRIGHTNESS), 0, 0]


def test_overflow_session_not_rendered():
    assert led_colors([_sess(None)]) == [0] * (NUM_KEY_LEDS * 3)


def test_oled_line_format():
    (line,) = oled_lines([_sess(2, Status.WAITING)])
    assert line == " 3 myproj       !"


def test_oled_truncates_to_eight_lines():
    sessions = [_sess(i, sid=f"s{i}") for i in range(10)]
    assert len(oled_lines(sessions)) == 8


def test_busy_session_lights_orange_with_tilde():
    colors = led_colors([_sess(1, Status.BUSY)])
    assert colors[3:6] == [int(255 * BRIGHTNESS), int(160 * BRIGHTNESS), 0]
    (line,) = oled_lines([_sess(1, Status.BUSY)])
    assert line == " 2 myproj       ~"


def test_out_of_range_slot_is_skipped():
    assert led_colors([_sess(16)]) == [0] * (NUM_KEY_LEDS * 3)
    assert led_colors([_sess(-1)]) == [0] * (NUM_KEY_LEDS * 3)
    assert oled_lines([_sess(16)]) == []


def test_unknown_session_lights_blue_with_question_mark():
    colors = led_colors([_sess(0, Status.UNKNOWN)])
    assert colors[0:3] == [0, 0, int(255 * BRIGHTNESS)]
    (line,) = oled_lines([_sess(0, Status.UNKNOWN)])
    assert line == " 1 myproj       ?"


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
