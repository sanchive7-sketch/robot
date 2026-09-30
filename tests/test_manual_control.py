import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main
from app.models import RobotMode


class FakeManualController:
    def __init__(self):
        self.mode = RobotMode.STOPPED
        self.directions = []
        self.disconnects = 0

    def set_mode(self, mode):
        self.mode = mode

    def manual_drive(self, direction):
        if self.mode != RobotMode.MANUAL:
            raise RuntimeError("Select Manual Control before moving the robot")
        self.directions.append(direction)

    def manual_connection_lost(self):
        if self.mode == RobotMode.MANUAL:
            self.mode = RobotMode.STOPPED
            self.disconnects += 1

    def snapshot(self):
        return {"mode": self.mode.value}


def prepare_manual_app(monkeypatch):
    controller = FakeManualController()
    monkeypatch.setattr(main, "controller", controller)
    monkeypatch.setattr(main, "_pin_matches", lambda pin: pin == "test-pin")
    monkeypatch.setattr(main, "_manual_socket", None)
    return controller, TestClient(main.app)


def test_mode_api_replaces_patrol_with_manual(monkeypatch) -> None:
    controller, client = prepare_manual_app(monkeypatch)

    response = client.post(
        "/api/mode", json={"mode": "manual", "pin": "test-pin"}
    )
    old_patrol = client.post(
        "/api/mode", json={"mode": "patrol", "pin": "test-pin"}
    )

    assert response.status_code == 200
    assert response.json() == {"mode": "manual"}
    assert controller.mode == RobotMode.MANUAL
    assert old_patrol.status_code == 400


def test_manual_websocket_is_authenticated_ordered_and_stops_on_disconnect(
    monkeypatch,
) -> None:
    controller, client = prepare_manual_app(monkeypatch)
    controller.mode = RobotMode.MANUAL

    with client.websocket_connect("/ws/manual") as websocket:
        websocket.send_json({"pin": "test-pin"})
        assert websocket.receive_json() == {"type": "ready"}

        websocket.send_json({"direction": "forward"})
        assert websocket.receive_json() == {
            "type": "ack",
            "direction": "forward",
        }
        websocket.send_json({"direction": "stop"})
        assert websocket.receive_json() == {"type": "ack", "direction": "stop"}

    assert controller.directions == ["forward", "stop"]
    assert controller.disconnects == 1
    assert controller.mode == RobotMode.STOPPED


def test_manual_websocket_rejects_an_incorrect_pin(monkeypatch) -> None:
    controller, client = prepare_manual_app(monkeypatch)

    with client.websocket_connect("/ws/manual") as websocket:
        websocket.send_json({"pin": "wrong"})
        assert websocket.receive_json() == {
            "type": "error",
            "message": "Incorrect control PIN",
        }
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()

    assert controller.directions == []
    assert controller.disconnects == 0
