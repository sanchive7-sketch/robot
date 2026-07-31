from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from .models import RobotMode, Telemetry, Visitor

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

    def __init__(
        self,
        link: SerialLink,
        vision: VisionService,
        speech: SarvamSpeech,
        llm: HybridLlm,
        event_store: EventStore,
    ):
        self.link = link
        self.vision = vision
        self.speech = speech
        self.llm = llm
        self.event_store = event_store
        self.mode = RobotMode.IDLE
        self.telemetry = Telemetry()
        self.activity = "Ready. Select Welcome or Patrol."
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interaction_active = False
        self._last_greeting_at = 0.0
        self._last_greeted_vip: str | None = None

    def start(self) -> None:
        self.vision.start()
        self.link.start()
        self._thread = threading.Thread(target=self._run, name="robot-control", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
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
            self.activity = text

    def set_mode(self, mode: RobotMode) -> None:
        with self._lock:
            if mode == RobotMode.STOPPED:
                self.link.stop()
                self.mode = RobotMode.STOPPED
                self.activity = "Stopped by remote control."
                return
            if self.mode == RobotMode.EMERGENCY:
                raise RuntimeError("Clear the physical fault before restarting")
            self.mode = mode
            self.activity = f"{mode.value.title()} mode active."
            self._interaction_active = False
            if mode == RobotMode.PATROL:
                self.link.set_mode("PATROL")
            elif mode == RobotMode.WELCOME:
                self.link.set_mode("WELCOME")
            else:
                self.link.stop()

    def emergency_stop(self, reason: str) -> None:
        with self._lock:
            self.link.stop()
            self.mode = RobotMode.EMERGENCY
            self.activity = f"Emergency stop: {reason}"

    def ask_text(self, question: str) -> str:
        answer = self.llm.answer(
            question, self.event_store.system_prompt(question)
        )
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
                "vip_name": visitor.vip_name if visitor else None,
                "identity_pending": visitor.identity_pending if visitor else False,
                "vision_backend": self.vision.active_backend,
                "vision_error": self.vision.last_error,
                "telemetry": self.telemetry.as_dict(),
            }

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                mode = self.mode
            if mode == RobotMode.WELCOME and not self._interaction_active:
                visitor = self.vision.get_visitor()
                if visitor:
                    self._approach_or_greet(visitor)

    def _approach_or_greet(self, visitor: Visitor) -> None:
        # Front ultrasonic is the final safety gate; face size is only an estimate.
        nearest_cm = min(
            self.telemetry.left_distance_cm, self.telemetry.right_distance_cm
        )
        if nearest_cm < self.MIN_OBSTACLE_CM:
            self.link.stop()
            if visitor.identity_pending:
                self.set_activity("Visitor reached; verifying VIP identity…")
                return
            self.set_activity("Visitor reached; maintaining a safe distance.")
            self._begin_interaction(visitor)
            return

        horizontal_error = visitor.center_x - 0.5
        if abs(horizontal_error) > 0.10:
            self.link.drive(0.0, -0.45 * horizontal_error / abs(horizontal_error))
            self.set_activity("Turning toward visitor…")
        elif visitor.proximity_fraction < self.APPROACH_PERSON_HEIGHT:
            self.link.drive(0.16, -0.55 * horizontal_error)
            self.set_activity("Approaching visitor slowly…")
        else:
            self.link.stop()
            if visitor.identity_pending:
                self.set_activity("Verifying VIP identity…")
                return
            self._begin_interaction(visitor)

    def _begin_interaction(self, visitor: Visitor) -> None:
        now = time.monotonic()
        # Avoid repeatedly greeting a person who remains in front of the camera.
        if now - self._last_greeting_at < 20:
            return
        if visitor.vip_id and visitor.vip_id == self._last_greeted_vip:
            if now - self._last_greeting_at < 90:
                return
        self._last_greeting_at = now
        self._last_greeted_vip = visitor.vip_id
        self._interaction_active = True
        threading.Thread(
            target=self._interaction,
            args=(visitor,),
            name="visitor-interaction",
            daemon=True,
        ).start()

    def _interaction(self, visitor: Visitor) -> None:
        try:
            self.link.stop()
            self.link.wave(2.5)
            greeting = visitor.vip_greeting or (
                f"Hello! Welcome to {self.event_store.event_name()}."
            )
            self._safe_speak(greeting)
            self._safe_speak("Can I help you to see our AI museum?")
            if not self.speech.configured:
                self.set_activity("Greeting complete. Add SARVAM_API_KEY for voice questions.")
                return

            first = self.speech.listen()
            if not first:
                self._safe_speak("I could not hear you. Please ask a volunteer if you need help.")
                return
            if self._is_no(first):
                self._safe_speak("No problem. Enjoy the event!")
                return
            if self._is_simple_yes(first):
                self._safe_speak("What would you like to know?")
                first = self.speech.listen()
            self._answer_and_offer_more(first)
        except Exception as exc:
            LOG.exception("Visitor interaction failed")
            self.set_activity(f"Speech error: {exc}")
            self._safe_speak("Sorry, I am having a connection problem. Please visit the help desk.")
        finally:
            self.link.stop()
            with self._lock:
                # "Ideal position" is a safe stationary idle state. Autonomous
                # return-to-home through a crowd needs a mapped navigation system.
                if self.mode != RobotMode.EMERGENCY:
                    self.mode = RobotMode.IDLE
                    self.activity = "Idle after visitor interaction."
                self._interaction_active = False

    def _answer_and_offer_more(self, question: str) -> None:
        if not question:
            return
        answer = self.llm.answer(
            question, self.event_store.system_prompt(question)
        )
        self._safe_speak(answer)
        self._safe_speak("Would you like to ask one more question?")
        follow_up = self.speech.listen()
        if not follow_up or self._is_no(follow_up):
            self._safe_speak("Thank you. Enjoy the AI museum!")
            return
        if self._is_simple_yes(follow_up):
            self._safe_speak("Please ask your question.")
            follow_up = self.speech.listen()
        if follow_up:
            answer = self.llm.answer(
                follow_up, self.event_store.system_prompt(follow_up)
            )
            self._safe_speak(answer)
            self._safe_speak("Thank you. Enjoy the AI museum!")

    def _safe_speak(self, text: str) -> None:
        try:
            self.speech.speak(text)
        except Exception:
            LOG.exception("TTS failed for: %s", text)

    @staticmethod
    def _is_no(text: str) -> bool:
        normalized = text.strip().lower().rstrip(".!?")
        return normalized in {
            "no",
            "no thanks",
            "no thank you",
            "not now",
            "வேண்டாம்",
            "नहीं",
        }

    @staticmethod
    def _is_simple_yes(text: str) -> bool:
        normalized = text.strip().lower().rstrip(".!?")
        return normalized in {"yes", "yes please", "sure", "okay", "ok", "ஆம்", "हाँ"}
