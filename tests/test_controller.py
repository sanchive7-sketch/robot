from app.controller import RobotController
from app.models import RobotMode, Telemetry, Visitor


class FakeLink:
    def __init__(self):
        self.commands = []

    def set_mode(self, mode):
        self.commands.append(("mode", mode))

    def stop(self):
        self.commands.append(("stop",))

    def drive(self, linear, angular):
        self.commands.append(("drive", linear, angular))

    def wave(self, seconds):
        self.commands.append(("wave", seconds))


class FakeVision:
    camera_ok = True

    def get_visitor(self):
        return None


class FakeSpeech:
    configured = False


class FakeLlm:
    def status(self):
        return {"last_provider": "none", "local_ready": False}

    def answer(self, question, system_prompt):
        return "Test answer"


class FakeEvent:
    pass


def make_controller() -> RobotController:
    return RobotController(
        FakeLink(), FakeVision(), FakeSpeech(), FakeLlm(), FakeEvent()
    )


def test_robot_starts_idle_and_does_not_move() -> None:
    controller = make_controller()

    assert controller.mode == RobotMode.IDLE
    assert controller.link.commands == []


def test_remote_stop_preempts_motion() -> None:
    controller = make_controller()
    controller.set_mode(RobotMode.WELCOME)
    controller.set_mode(RobotMode.STOPPED)

    assert controller.mode == RobotMode.STOPPED
    assert controller.link.commands[-1] == ("stop",)


def test_approach_is_blocked_by_ultrasonic_clearance() -> None:
    controller = make_controller()
    controller.telemetry = Telemetry(left_distance_cm=55, right_distance_cm=120)
    controller._begin_interaction = lambda visitor: controller.link.commands.append(
        ("interact",)
    )
    visitor = Visitor(center_x=0.5, center_y=0.5, proximity_fraction=0.1)

    controller._approach_or_greet(visitor)

    assert ("drive", 0.16, 0.0) not in controller.link.commands
    assert controller.link.commands[-2:] == [("stop",), ("interact",)]


def test_greeting_waits_for_multi_frame_vip_confirmation() -> None:
    controller = make_controller()
    controller.telemetry = Telemetry(left_distance_cm=55, right_distance_cm=120)
    controller._begin_interaction = lambda visitor: controller.link.commands.append(
        ("interact",)
    )
    visitor = Visitor(
        center_x=0.5,
        center_y=0.5,
        proximity_fraction=0.9,
        identity_pending=True,
    )

    controller._approach_or_greet(visitor)

    assert ("interact",) not in controller.link.commands
    assert controller.link.commands[-1] == ("stop",)
