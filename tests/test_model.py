from agent_monitor.model import Session, Status, status_for_event


def test_session_start_is_available():
    assert status_for_event("SessionStart", None, None) == Status.AVAILABLE


def test_user_prompt_submit_is_busy():
    assert status_for_event("UserPromptSubmit", None, Status.AVAILABLE) == Status.BUSY


def test_stop_is_available():
    assert status_for_event("Stop", None, Status.BUSY) == Status.AVAILABLE


def test_permission_notification_is_waiting():
    msg = "Claude needs your permission to use Bash"
    assert status_for_event("Notification", msg, Status.BUSY) == Status.WAITING


def test_idle_notification_keeps_current_status():
    msg = "Claude is waiting for your input"
    assert status_for_event("Notification", msg, Status.AVAILABLE) is None


def test_unknown_notification_defaults_to_waiting():
    assert status_for_event("Notification", "Something else", Status.BUSY) == Status.WAITING


def test_unknown_event_returns_none():
    assert status_for_event("PreCompact", None, Status.BUSY) is None


def test_idle_notification_case_insensitive():
    assert status_for_event("Notification", "WAITING FOR YOUR INPUT", Status.AVAILABLE) is None


def test_notification_without_message_defaults_to_waiting():
    assert status_for_event("Notification", None, Status.BUSY) == Status.WAITING


def test_session_remote_defaults_false_and_roundtrips():
    sess = Session("a", "/proj/a", 100, Status.AVAILABLE, 0, 1.0)
    assert sess.remote is False
    assert sess.to_dict()["remote"] is False

    sess.remote = True
    d = sess.to_dict()
    assert d["remote"] is True
    assert Session.from_dict(d).remote is True

    assert Session.from_dict({"session_id": "b", "status": "busy"}).remote is False
