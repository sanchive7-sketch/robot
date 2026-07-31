from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable

import serial

from .models import Telemetry

LOG = logging.getLogger(__name__)


class SerialLink:
    """Newline-delimited JSON link. A write failure always marks the robot disconnected."""

    def __init__(
        self,
        port: str,
        baud: int,
        simulation: bool,
        on_telemetry: Callable[[Telemetry], None],
    ):
        self.port = port
        self.baud = baud
        self.simulation = simulation
        self.on_telemetry = on_telemetry
        self._serial: serial.Serial | None = None
        self._stop = threading.Event()
        self._tx: queue.Queue[dict] = queue.Queue(maxsize=50)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="esp32-link", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self.send({"cmd": "STOP"})
        if self._thread:
            self._thread.join(timeout=2)
        if self._serial:
            self._serial.close()

    def send(self, message: dict) -> None:
        try:
            self._tx.put_nowait(message)
        except queue.Full:
            LOG.warning("Serial command queue full; dropping %s", message.get("cmd"))

    def set_mode(self, mode: str) -> None:
        self.send({"cmd": "SET_MODE", "mode": mode.upper()})

    def stop(self) -> None:
        # Place STOP at the head by clearing stale motion commands.
        while True:
            try:
                self._tx.get_nowait()
            except queue.Empty:
                break
        self.send({"cmd": "STOP"})

    def drive(self, linear_mps: float, angular_rps: float) -> None:
        self.send(
            {
                "cmd": "DRIVE",
                "linear_mps": round(linear_mps, 3),
                "angular_rps": round(angular_rps, 3),
            }
        )

    def wave(self, seconds: float = 2.5) -> None:
        self.send({"cmd": "WAVE", "seconds": seconds})

    def _run(self) -> None:
        if self.simulation:
            self._run_simulation()
            return

        try:
            self._serial = serial.Serial(self.port, self.baud, timeout=0.1)
            self._serial.reset_input_buffer()
            LOG.info("Connected to ESP32 on %s", self.port)
            last_heartbeat = 0.0
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_heartbeat >= 0.25:
                    self.send({"cmd": "HEARTBEAT"})
                    last_heartbeat = now
                self._flush_one()
                raw = self._serial.readline()
                if raw:
                    self._handle_line(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            LOG.exception("ESP32 serial link failed")
            self.on_telemetry(Telemetry(connected=False, last_error=str(exc)))

    def _flush_one(self) -> None:
        if not self._serial:
            return
        try:
            message = self._tx.get_nowait()
        except queue.Empty:
            return
        self._serial.write((json.dumps(message) + "\n").encode("utf-8"))

    def _handle_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
            if payload.get("type") != "telemetry":
                return
            self.on_telemetry(
                Telemetry(
                    x_m=float(payload.get("x_m", 0)),
                    y_m=float(payload.get("y_m", 0)),
                    heading_rad=float(payload.get("heading_rad", 0)),
                    left_distance_cm=float(payload.get("left_cm", 999)),
                    right_distance_cm=float(payload.get("right_cm", 999)),
                    battery_v=float(payload.get("battery_v", 0)),
                    connected=True,
                    last_error=str(payload.get("error", "")),
                )
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            LOG.warning("Bad ESP32 message: %r", line[:200])

    def _run_simulation(self) -> None:
        LOG.warning("ROBOT_SIMULATION=true: motors will not move")
        while not self._stop.wait(0.25):
            while True:
                try:
                    message = self._tx.get_nowait()
                    LOG.info("SIM ESP32 <- %s", message)
                except queue.Empty:
                    break
            # Simulation accepts commands but no physical ESP32 is attached.
            self.on_telemetry(Telemetry(connected=False, battery_v=12.0))
        time.sleep(0.05)
