from __future__ import annotations

import json

from app.wifi_link import WifiLink


def make_link(received: list) -> WifiLink:
    return WifiLink("unused", 0, False, received.append)


def test_authenticated_telemetry_connects_and_records_sender(monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_WIFI_TOKEN", "test-token")
    received = []
    link = make_link(received)

    link._handle_datagram(
        json.dumps({"type": "telemetry", "token": "test-token", "left_cm": 12.5}).encode(),
        ("192.168.1.44", 9010),
    )

    assert link._endpoint == ("192.168.1.44", 9010)
    assert received[-1].connected is True
    assert received[-1].left_distance_cm == 12.5


def test_wrong_token_cannot_set_esp32_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_WIFI_TOKEN", "test-token")
    received = []
    link = make_link(received)

    link._handle_datagram(
        b'{"type":"telemetry","token":"wrong-token"}', ("192.168.1.99", 9010)
    )

    assert link._endpoint is None
    assert received == []


def test_wifi_manual_direction_replaces_stale_manual_command(monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_WIFI_TOKEN", "test-token")
    link = make_link([])
    link.set_mode("manual")
    link.manual_drive(0.10, 0.0)
    link.manual_drive(0.0, -0.45)

    assert link._tx.get_nowait() == {"cmd": "SET_MODE", "mode": "MANUAL"}
    assert link._tx.get_nowait() == {
        "cmd": "MANUAL_DRIVE",
        "linear_mps": 0.0,
        "angular_rps": -0.45,
    }
