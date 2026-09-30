from __future__ import annotations

import logging
import math
import threading
import time
from typing import TYPE_CHECKING

from .models import RobotMode, Telemetry, Visitor
from .speech import SpeechError

if TYPE_CHECKING:
    from .event_store import EventStore
    from .llm import HybridLlm
    from .serial_link import SerialLink
    from .speech import SarvamSpeech
    from .vision import VisionService

LOG = logging.getLogger(__name__)


class RobotController:
    # Fraction of frame height occupied by the detected person. Calibrate this
    # with the final phone position; ultrasonic distance remains the safety gate.
    APPROACH_PERSON_HEIGHT = 0.65
    MIN_OBSTACLE_CM = 70.0
    WELCOME_ZONE_MARGIN_M = 0.06
    MANUAL_DIRECTIONS = {
        "forward": (0.10, 0.0),
        "backward": (-0.07, 0.0),
        "left": (0.0, 0.45),
        "right": (0.0, -0.45),
    }

    def __init__(
        self,
        link: SerialLink,
        vision: VisionService,
        speech: SarvamSpeech,
        llm: HybridLlm,
        event_store: EventStore,
        welcome_area_forward_m: float = 1.20,
        welcome_area_side_m: float = 0.60,
        welcome_home_tolerance_m: float = 0.08,
        welcome_return_timeout_seconds: float = 20.0,
    ):
        if welcome_area_forward_m <= self.WELCOME_ZONE_MARGIN_M:
            raise ValueError("welcome_area_forward_m must be greater than the safety margin")
        if welcome_area_side_m <= self.WELCOME_ZONE_MARGIN_M:
            raise ValueError("welcome_area_side_m must be greater than the safety margin")
        self.link = link
        self.vision = vision
        self.speech = speech
        self.llm = llm
        self.event_store = event_store
        self.welcome_area_forward_m = welcome_area_forward_m
        self.welcome_area_side_m = welcome_area_side_m
        self.welcome_home_tolerance_m = welcome_home_tolerance_m
        self.welcome_return_timeout_seconds = welcome_return_timeout_seconds
        self.mode = RobotMode.IDLE
        self.telemetry = Telemetry()
        self.activity = "Ready. Select Welcome or Manual Control."
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interaction_active = False
        self._interaction_cancel: threading.Event | None = None
        self._interaction_thread: threading.Thread | None = None
        self._last_greeting_at = 0.0
        self._welcome_home: tuple[float, float, float] | None = None
        self._wait_for_visitor_to_leave = False

    def start(self) -> None:
        self.vision.start()
        self.link.start()
        self._thread = threading.Thread(target=self._run, name="robot-control", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self.speech.cancel()
        with self._lock:
            self._cancel_interaction_locked()
        self.link.stop()
        if self._thread:
            self._thread.join(timeout=2)
        self.vision.close()
        self.link.close()

    def update_telemetry(self, telemetry: Telemetry) -> None:
        with self._lock:
            self.telemetry = telemetry
        if telemetry.last_error:
            self.emergency_stop(telemetry.last_error)

    def set_activity(self, text: str) -> None:
        with self._lock:
            if self.mode in {RobotMode.STOPPED, RobotMode.EMERGENCY}:
                return
            self.activity = text

    def set_mode(self, mode: RobotMode) -> None:
        # Stop laptop audio/recording before publishing the new mode. Sarvam
        # requests already in flight may finish, but their cancelled session is
        # prevented from playing or advancing the conversation.
        self.speech.cancel()
        with self._lock:
            self._cancel_interaction_locked()
            if mode == RobotMode.STOPPED:
                self.link.stop()
                self.mode = RobotMode.STOPPED
                self.activity = "Stopped by remote control."
                return
            if self.mode == RobotMode.EMERGENCY:
                raise RuntimeError("Clear the physical fault before restarting")
            # Every mode transition starts from stopped motors and an empty
            # motion queue. This prevents an old manual command reaching the
            # ESP32 after Welcome mode has been selected.
            self.link.stop()
            self.mode = mode
            self.activity = f"{mode.value.title()} mode active."
            if mode == RobotMode.MANUAL:
                self.link.set_mode("MANUAL")
                self.activity = "Manual control ready. Hold a direction to move."
            elif mode == RobotMode.WELCOME:
                # The welcome zone and return point are measured from where the
                # operator enabled Welcome mode, never from an arbitrary map pose.
                self._welcome_home = self._current_pose()
                self._wait_for_visitor_to_leave = False
                self._last_greeting_at = 0.0
                configure_zone = getattr(self.link, "set_welcome_zone", None)
                if configure_zone:
                    configure_zone(self.welcome_area_forward_m, self.welcome_area_side_m)
                self.link.set_mode("WELCOME")
            else:
                self.link.stop()

    def manual_drive(self, direction: str) -> None:
        """Apply one fixed-speed, hold-to-move command in Manual mode only."""
        direction = direction.strip().lower()
        with self._lock:
            if self.mode != RobotMode.MANUAL:
                self.link.stop()
                raise RuntimeError("Select Manual Control before moving the robot")
            if direction == "stop":
                self.link.manual_drive(0.0, 0.0)
                self.activity = "Manual control ready. Hold a direction to move."
                return
            command = self.MANUAL_DIRECTIONS.get(direction)
            if command is None:
                self.link.manual_drive(0.0, 0.0)
                raise ValueError("direction must be forward, backward, left, right, or stop")
            if direction == "forward" and min(
                self.telemetry.left_distance_cm,
                self.telemetry.right_distance_cm,
            ) < self.MIN_OBSTACLE_CM:
                self.link.manual_drive(0.0, 0.0)
                self.activity = "Manual forward movement blocked by an obstacle."
                raise RuntimeError("Forward movement blocked by an obstacle")
            linear_mps, angular_rps = command
            self.link.manual_drive(linear_mps, angular_rps)
            self.activity = f"Manual control: moving {direction}."

    def manual_connection_lost(self) -> None:
        """Stop a disconnected manual session without interrupting another mode."""
        with self._lock:
            if self.mode != RobotMode.MANUAL:
                return
            self.link.stop()
            self.mode = RobotMode.STOPPED
            self.activity = "Manual controller disconnected; motors stopped."

    def emergency_stop(self, reason: str) -> None:
        self.speech.cancel()
        with self._lock:
            self._cancel_interaction_locked()
            self.link.stop()
            self.mode = RobotMode.EMERGENCY
            self.activity = f"Emergency stop: {reason}"

    def ask_text(self, question: str) -> str:
        answer = self.llm.answer(question, self.event_store.system_prompt(question))
        threading.Thread(target=self._safe_speak, args=(answer,), daemon=True).start()
        return answer

    def snapshot(self) -> dict:
        visitor = self.vision.get_visitor()
        with self._lock:
            return {
                "mode": self.mode.value,
                "activity": self.activity,
                "simulation": self.link.simulation,
                "camera_ok": self.vision.camera_ok,
                "speech_configured": self.speech.configured,
                "llm": self.llm.status(),
                "visitor_detected": visitor is not None,
                "visitor_count": visitor.person_count if visitor else 0,
                "vision_backend": self.vision.active_backend,
                "vision_error": self.vision.last_error,
                "telemetry": self.telemetry.as_dict(),
            }

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                mode = self.mode
                interaction_active = self._interaction_active
                wait_for_departure = self._wait_for_visitor_to_leave
            if mode != RobotMode.WELCOME or interaction_active:
                continue
            visitor = self.vision.get_visitor()
            if wait_for_departure:
                if visitor is None:
                    with self._lock:
                        self._wait_for_visitor_to_leave = False
                        self.activity = "Welcome zone ready for the next visitor."
                continue
            if visitor:
                self._approach_or_greet(visitor)

    def _approach_or_greet(self, visitor: Visitor) -> None:
        # Detection alone starts the conversation. Welcome mode never drives or
        # turns toward a visitor, regardless of bounding-box size or position.
        self.link.stop()
        self.set_activity("Visitor detected; starting conversation.")
        self._begin_interaction(visitor)

    def _begin_interaction(self, visitor: Visitor) -> None:
        now = time.monotonic()
        with self._lock:
            if self.mode != RobotMode.WELCOME or self._interaction_active:
                return
            # Vision can publish the same frame twice. The later clearance gate
            # prevents a completed conversation from greeting that same group again.
            if now - self._last_greeting_at < 2:
                return
            self._last_greeting_at = now
            cancel_event = threading.Event()
            self._interaction_cancel = cancel_event
            self._interaction_active = True
            interaction_thread = threading.Thread(
                target=self._interaction,
                args=(visitor, cancel_event),
                name="visitor-interaction",
                daemon=True,
            )
            self._interaction_thread = interaction_thread
        interaction_thread.start()

    def _interaction(
        self, visitor: Visitor, cancel_event: threading.Event
    ) -> None:
        interaction_error = None
        try:
            if cancel_event.is_set():
                return
            self.link.stop()
            self.link.wave(2.5)
            if not self._speak(self._greeting_for(visitor), cancel_event):
                return
            if not self._speak("How can I help you?", cancel_event):
                return
            if not self.speech.configured:
                if not cancel_event.is_set():
                    self.set_activity(
                        "Greeting complete. Add SARVAM_API_KEY for voice questions."
                    )
                return

            first = self.speech.listen(cancel_event=cancel_event)
            if cancel_event.is_set():
                return
            if not first:
                self._speak(
                    "I could not hear you. Please ask a volunteer if you need help.",
                    cancel_event,
                )
                return
            if self._is_no(first):
                self._speak("No problem. Enjoy the event!", cancel_event)
                return
            if self._is_simple_yes(first):
                if not self._speak(
                    "What would you like to know?", cancel_event
                ):
                    return
                first = self.speech.listen(cancel_event=cancel_event)
                if cancel_event.is_set():
                    return
            self._answer_and_offer_more(first, cancel_event)
        except Exception as exc:
            if cancel_event.is_set():
                return
            LOG.exception("Visitor interaction failed")
            interaction_error = f"Speech error: {exc}"
            self.set_activity(interaction_error)
            if isinstance(exc, SpeechError):
                self._safe_speak(
                    "Sorry, I cannot hear you right now. Please visit the help desk.",
                    cancel_event,
                )
            else:
                self._safe_speak(
                    "Sorry, I am having a connection problem. Please visit the help desk.",
                    cancel_event,
                )
        finally:
            with self._lock:
                is_current = self._interaction_cancel is cancel_event
                if is_current:
                    self.link.stop()
            if not is_current:
                return
            if cancel_event.is_set() or interaction_error:
                returned_home = False
            else:
                returned_home = self._return_to_welcome_home()
            with self._lock:
                if self._interaction_cancel is not cancel_event:
                    return
                if self.mode == RobotMode.WELCOME:
                    if not cancel_event.is_set():
                        self._wait_for_visitor_to_leave = True
                        self.activity = (
                            "Back at the welcome position; waiting for visitors to leave."
                            if returned_home
                            else "Stopped safely; unable to confirm return to welcome position."
                        )
                elif self.mode == RobotMode.IDLE:
                    self.activity = "Idle after visitor interaction."
                if interaction_error:
                    self.activity = interaction_error
                self._interaction_active = False
                self._interaction_cancel = None
                self._interaction_thread = None

    def _answer_and_offer_more(
        self, question: str, cancel_event: threading.Event
    ) -> None:
        while question and not cancel_event.is_set():
            answer = self.llm.answer(question, self.event_store.system_prompt(question))
            if cancel_event.is_set() or not self._speak(answer, cancel_event):
                return
            if not self._speak(
                "Is there anything else I can help you with?", cancel_event
            ):
                return
            question = self.speech.listen(cancel_event=cancel_event)
            if cancel_event.is_set():
                return
            if not question or self._is_no(question):
                self._speak("Thank you. Enjoy the AI museum!", cancel_event)
                return
            if self._is_simple_yes(question):
                if not self._speak("Please ask your question.", cancel_event):
                    return
                question = self.speech.listen(cancel_event=cancel_event)
        if not cancel_event.is_set():
            self._speak("Thank you. Enjoy the AI museum!", cancel_event)

    def _greeting_for(self, visitor: Visitor) -> str:
        if visitor.person_count > 1:
            return f"Hello everyone! Welcome you all to {self.event_store.event_name()}."
        return f"Hello! Welcome to {self.event_store.event_name()}."

    def _current_pose(self) -> tuple[float, float, float]:
        with self._lock:
            return (self.telemetry.x_m, self.telemetry.y_m, self.telemetry.heading_rad)

    def _can_move_within_welcome_zone(self, linear_mps: float) -> bool:
        """Keep host-side approach commands inside the ESP32-enforced zone."""
        home = self._welcome_home
        if home is None:
            return False
        x, y, heading = self._current_pose()
        # Check a short command horizon, leaving room for encoder and command
        # latency. The firmware repeats the same check as the final guard.
        x += linear_mps * 0.5 * math.cos(heading)
        y += linear_mps * 0.5 * math.sin(heading)
        dx, dy = x - home[0], y - home[1]
        forward = dx * math.cos(home[2]) + dy * math.sin(home[2])
        side = -dx * math.sin(home[2]) + dy * math.cos(home[2])
        return (
            abs(forward) <= self.welcome_area_forward_m - self.WELCOME_ZONE_MARGIN_M
            and abs(side) <= self.welcome_area_side_m - self.WELCOME_ZONE_MARGIN_M
        )

    def _return_to_welcome_home(self) -> bool:
        """Return by odometry within the configured zone, or stop if unsafe."""
        home = self._welcome_home
        if home is None:
            return False
        with self._lock:
            if self.mode != RobotMode.WELCOME:
                self.link.stop()
                return False
        if getattr(self.link, "simulation", False):
            self.link.stop()
            return True
        with self._lock:
            connected = self.telemetry.connected
        if not connected:
            self.link.stop()
            return False

        deadline = time.monotonic() + self.welcome_return_timeout_seconds
        while time.monotonic() < deadline:
            with self._lock:
                if self.mode != RobotMode.WELCOME:
                    self.link.stop()
                    return False
                telemetry = self.telemetry
            if min(telemetry.left_distance_cm, telemetry.right_distance_cm) < self.MIN_OBSTACLE_CM:
                self.link.stop()
                self.set_activity("Return paused: obstacle inside safety clearance.")
                return False

            dx, dy = home[0] - telemetry.x_m, home[1] - telemetry.y_m
            distance = math.hypot(dx, dy)
            if distance <= self.welcome_home_tolerance_m:
                heading_error = self._wrap_angle(home[2] - telemetry.heading_rad)
                if abs(heading_error) <= 0.10:
                    self.link.stop()
                    return True
                self.link.drive(0.0, max(-0.40, min(0.40, heading_error * 1.4)))
            else:
                target_heading = math.atan2(dy, dx)
                heading_error = self._wrap_angle(target_heading - telemetry.heading_rad)
                if abs(heading_error) > 0.16:
                    self.link.drive(0.0, max(-0.40, min(0.40, heading_error * 1.4)))
                elif self._can_move_within_welcome_zone(0.12):
                    self.link.drive(0.12, max(-0.25, min(0.25, heading_error * 0.8)))
                else:
                    self.link.stop()
                    self.set_activity("Return stopped at the welcome-area boundary.")
                    return False
            time.sleep(0.1)
        self.link.stop()
        self.set_activity("Return timed out; robot stopped safely.")
        return False

    @staticmethod
    def _wrap_angle(angle: float) -> float:
        while angle > math.pi:
            angle -= 2.0 * math.pi
        while angle < -math.pi:
            angle += 2.0 * math.pi
        return angle

    def _cancel_interaction_locked(self) -> None:
        """Invalidate the current visitor session while holding ``self._lock``."""
        if self._interaction_cancel is not None:
            self._interaction_cancel.set()
        self._interaction_cancel = None
        self._interaction_active = False
        self._interaction_thread = None

    def _speak(
        self,
        text: str,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return False
        return self.speech.speak(
            self.event_store.spoken_text(text),
            cancel_event=cancel_event,
        )

    def _safe_speak(
        self,
        text: str,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        try:
            return self._speak(text, cancel_event)
        except Exception:
            if cancel_event is not None and cancel_event.is_set():
                return False
            LOG.exception("TTS failed for: %s", text)
            return False

    @staticmethod
    def _is_no(text: str) -> bool:
        normalized = text.strip().lower().rstrip(".!?")
        return normalized in {"no", "no thanks", "no thank you", "not now", "வேண்டாம்", "नहीं"}

    @staticmethod
    def _is_simple_yes(text: str) -> bool:
        normalized = text.strip().lower().rstrip(".!?")
        return normalized in {"yes", "yes please", "sure", "okay", "ok", "ஆம்", "हाँ"}
