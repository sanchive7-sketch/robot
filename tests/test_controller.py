import threading
import time

from app.controller import RobotController
from app.models import RobotMode, Telemetry, Visitor


class FakeLink:
    def __init__(self):
        self.commands = []
        self.simulation = True

    def set_mode(self, mode):
        self.commands.append(("mode", mode))

    def set_welcome_zone(self, forward, side):
        self.commands.append(("welcome_zone", forward, side))

    def stop(self):
        self.commands.append(("stop",))

    def drive(self, linear, angular):
        self.commands.append(("drive", linear, angular))

    def manual_drive(self, linear, angular):
        self.commands.append(("manual_drive", linear, angular))

    def wave(self, seconds):
        self.commands.append(("wave", seconds))


class FakeVision:
    camera_ok = True

    def get_visitor(self):
        return None


class FakeSpeech:
    def __init__(self, configured=False, transcripts=()):
        self.configured = configured
        self.transcripts = list(transcripts)
        self.spoken = []
        self.cancel_calls = 0

    def speak(self, text, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return False
        self.spoken.append(text)
        return True

    def listen(self, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return ""
        return self.transcripts.pop(0) if self.transcripts else ""

    def cancel(self):
        self.cancel_calls += 1


class FakeLlm:
    def __init__(self):
        self.questions = []

    def status(self):
        return {"last_provider": "none", "local_ready": False}

    def answer(self, question, system_prompt):
        self.questions.append(question)
        return "Test answer"


class FakeEvent:
    def event_name(self):
        return "Test Event"

    def system_prompt(self, question):
        return f"Prompt for {question}"

    def spoken_text(self, text):
        return text


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


def test_remote_stop_cancels_conversation_and_welcome_starts_fresh() -> None:
    class BlockingSpeech(FakeSpeech):
        def __init__(self):
            super().__init__(configured=False)
            self.first_speech_started = threading.Event()
            self.first_speech_returned = threading.Event()

        def speak(self, text, cancel_event=None):
            self.spoken.append(text)
            if len(self.spoken) == 1:
                self.first_speech_started.set()
                assert cancel_event is not None
                assert cancel_event.wait(timeout=2)
                self.first_speech_returned.set()
                return False
            return not (cancel_event is not None and cancel_event.is_set())

    controller = make_controller()
    controller.speech = BlockingSpeech()
    visitor = Visitor(center_x=0.5, center_y=0.5, proximity_fraction=0.9)
    controller.set_mode(RobotMode.WELCOME)
    controller._begin_interaction(visitor)
    assert controller.speech.first_speech_started.wait(timeout=2)
    cancel_calls_before_stop = controller.speech.cancel_calls

    controller.set_mode(RobotMode.STOPPED)

    assert controller.speech.first_speech_returned.wait(timeout=2)
    time.sleep(0.05)
    assert controller.mode == RobotMode.STOPPED
    assert controller.activity == "Stopped by remote control."
    assert controller.speech.cancel_calls == cancel_calls_before_stop + 1
    assert controller.speech.spoken == ["Hello! Welcome to Test Event."]

    controller.set_mode(RobotMode.WELCOME)
    controller._begin_interaction(visitor)
    deadline = time.monotonic() + 2
    while len(controller.speech.spoken) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert controller.speech.spoken == [
        "Hello! Welcome to Test Event.",
        "Hello! Welcome to Test Event.",
        "How can I help you?",
    ]
    assert controller.mode == RobotMode.WELCOME


def test_any_detected_person_starts_conversation_without_approach_motion() -> None:
    controller = make_controller()
    controller.telemetry = Telemetry(left_distance_cm=999, right_distance_cm=999)
    controller._begin_interaction = lambda visitor: controller.link.commands.append(
        ("interact",)
    )
    visitor = Visitor(center_x=0.95, center_y=0.1, proximity_fraction=0.01)

    controller._approach_or_greet(visitor)

    assert not any(command[0] == "drive" for command in controller.link.commands)
    assert controller.link.commands[-2:] == [("stop",), ("interact",)]


def test_single_visitor_receives_the_same_generic_greeting() -> None:
    controller = make_controller()
    visitor = Visitor(
        center_x=0.5,
        center_y=0.5,
        proximity_fraction=0.9,
    )

    assert controller._greeting_for(visitor) == "Hello! Welcome to Test Event."


def test_manual_mode_starts_stopped_and_uses_fixed_direction_speeds() -> None:
    controller = make_controller()

    controller.set_mode(RobotMode.MANUAL)
    controller.manual_drive("forward")
    controller.manual_drive("left")
    controller.manual_drive("stop")

    assert controller.link.commands == [
        ("stop",),
        ("mode", "MANUAL"),
        ("manual_drive", 0.10, 0.0),
        ("manual_drive", 0.0, 0.45),
        ("manual_drive", 0.0, 0.0),
    ]


def test_manual_movement_is_rejected_outside_manual_mode() -> None:
    controller = make_controller()

    try:
        controller.manual_drive("forward")
    except RuntimeError as exc:
        assert str(exc) == "Select Manual Control before moving the robot"
    else:
        raise AssertionError("manual movement must require Manual mode")

    assert controller.link.commands == [("stop",)]


def test_manual_forward_is_blocked_by_front_clearance() -> None:
    controller = make_controller()
    controller.set_mode(RobotMode.MANUAL)
    controller.telemetry = Telemetry(left_distance_cm=45, right_distance_cm=120)

    try:
        controller.manual_drive("forward")
    except RuntimeError as exc:
        assert str(exc) == "Forward movement blocked by an obstacle"
    else:
        raise AssertionError("forward movement must stop for an obstacle")

    assert controller.link.commands[-1] == ("manual_drive", 0.0, 0.0)
    assert not any(
        command[0] == "manual_drive" and command != ("manual_drive", 0.0, 0.0)
        for command in controller.link.commands
    )


def test_manual_disconnect_stops_manual_but_does_not_interrupt_welcome() -> None:
    controller = make_controller()
    controller.set_mode(RobotMode.MANUAL)

    controller.manual_connection_lost()

    assert controller.mode == RobotMode.STOPPED
    assert controller.link.commands[-1] == ("stop",)

    controller.set_mode(RobotMode.WELCOME)
    command_count = len(controller.link.commands)
    controller.manual_connection_lost()

    assert controller.mode == RobotMode.WELCOME
    assert len(controller.link.commands) == command_count


def test_voice_generation_failure_is_not_hidden_by_return_home_status() -> None:
    class FailingSpeech(FakeSpeech):
        configured = True

        def speak(self, text, cancel_event=None):
            raise RuntimeError("402 Payment Required")

    controller = make_controller()
    controller.speech = FailingSpeech()
    controller.set_mode(RobotMode.WELCOME)
    visitor = Visitor(center_x=0.5, center_y=0.5, proximity_fraction=0.5)
    cancel_event = threading.Event()
    controller._interaction_cancel = cancel_event
    controller._interaction_active = True

    controller._interaction(visitor, cancel_event)

    assert controller.activity == "Speech error: 402 Payment Required"
    assert controller.activity != "Back at the welcome position; waiting for visitors to leave."


def test_group_greeting_and_repeated_voice_questions_use_answer_pipeline() -> None:
    controller = make_controller()
    controller.speech = FakeSpeech(
        configured=True,
        transcripts=("Where is registration?", "yes", "When does it close?", "no"),
    )
    visitor = Visitor(
        center_x=0.5,
        center_y=0.5,
        proximity_fraction=0.9,
        person_count=3,
    )

    controller.set_mode(RobotMode.WELCOME)
    cancel_event = threading.Event()
    controller._interaction_cancel = cancel_event
    controller._interaction_active = True
    controller._interaction(visitor, cancel_event)

    assert controller.llm.questions == ["Where is registration?", "When does it close?"]
    assert controller.speech.spoken[0] == "Hello everyone! Welcome you all to Test Event."
    assert controller.speech.spoken.count("Is there anything else I can help you with?") == 2
