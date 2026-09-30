import queue

from app.serial_link import SerialLink


def make_link() -> SerialLink:
    return SerialLink("COM0", 115200, True, lambda telemetry: None)


def test_manual_drive_uses_a_separate_firmware_command() -> None:
    link = make_link()

    link.manual_drive(0.10, -0.45)

    assert link._tx.get_nowait() == {
        "cmd": "MANUAL_DRIVE",
        "linear_mps": 0.10,
        "angular_rps": -0.45,
    }


def test_stop_discards_all_pending_manual_motion() -> None:
    link = make_link()
    link.manual_drive(0.10, 0.0)
    link.manual_drive(0.0, 0.45)
    link.send({"cmd": "HEARTBEAT"})

    link.stop()

    assert link._tx.get_nowait() == {"cmd": "STOP"}
    try:
        link._tx.get_nowait()
    except queue.Empty:
        pass
    else:
        raise AssertionError("STOP must remove stale movement commands")


def test_new_manual_direction_replaces_stale_motion_but_preserves_mode() -> None:
    link = make_link()
    link.set_mode("MANUAL")
    link.manual_drive(0.10, 0.0)

    link.manual_drive(0.0, -0.45)

    assert link._tx.get_nowait() == {"cmd": "SET_MODE", "mode": "MANUAL"}
    assert link._tx.get_nowait() == {
        "cmd": "MANUAL_DRIVE",
        "linear_mps": 0.0,
        "angular_rps": -0.45,
    }
    try:
        link._tx.get_nowait()
    except queue.Empty:
        pass
    else:
        raise AssertionError("stale manual direction must be replaced")
