"""Authenticated UDP transport for the Wi-Fi ESP32 firmware variant.

The original ``SerialLink`` remains unchanged.  ``app.wifi_main`` injects this
class only when the Wi-Fi dashboard entry point is used.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import time
from collections.abc import Callable

from .models import Telemetry

LOG = logging.getLogger(__name__)


class WifiLink:
    """JSON-over-UDP link that learns the ESP32 address from authenticated telemetry."""

    def __init__(
        self,
        _serial_port: str,
        _serial_baud: int,
        simulation: bool,
        on_telemetry: Callable[[Telemetry], None],
    ) -> None:
        self.simulation = simulation
        self.on_telemetry = on_telemetry
        self.bind_host = os.getenv("ROBOT_WIFI_BIND_HOST", "0.0.0.0").strip()
        self.telemetry_port = int(os.getenv("ROBOT_WIFI_TELEMETRY_PORT", "9011"))
        self.token = os.getenv("ROBOT_WIFI_TOKEN", "").strip()
        self._endpoint: tuple[str, int] | None = None
        self._last_seen = 0.0
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tx: queue.Queue[dict] = queue.Queue(maxsize=50)
        self._tx_lock = threading.Lock()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="esp32-wifi-link", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.stop()
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._socket:
            self._socket.close()
            self._socket = None

    def send(self, message: dict) -> None:
        with self._tx_lock:
            self._send_unlocked(message)

    def _send_unlocked(self, message: dict) -> None:
        try:
            self._tx.put_nowait(message)
        except queue.Full:
            LOG.warning("Wi-Fi command queue full; dropping %s", message.get("cmd"))

    def set_mode(self, mode: str) -> None:
        self.send({"cmd": "SET_MODE", "mode": mode.upper()})

    def set_welcome_zone(self, forward_m: float, side_m: float) -> None:
        self.send(
            {
                "cmd": "SET_WELCOME_ZONE",
                "forward_limit_m": round(forward_m, 3),
                "side_limit_m": round(side_m, 3),
            }
        )

    def stop(self) -> None:
        with self._tx_lock:
            while True:
                try:
                    self._tx.get_nowait()
                except queue.Empty:
                    break
            self._send_unlocked({"cmd": "STOP"})

    def drive(self, linear_mps: float, angular_rps: float) -> None:
        self.send(
            {
                "cmd": "DRIVE",
                "linear_mps": round(linear_mps, 3),
                "angular_rps": round(angular_rps, 3),
            }
        )

    def manual_drive(self, linear_mps: float, angular_rps: float) -> None:
        command = {
            "cmd": "MANUAL_DRIVE",
            "linear_mps": round(linear_mps, 3),
            "angular_rps": round(angular_rps, 3),
        }
        with self._tx_lock:
            preserved = []
            while True:
                try:
                    pending = self._tx.get_nowait()
                except queue.Empty:
                    break
                if pending.get("cmd") != "MANUAL_DRIVE":
                    preserved.append(pending)
            for pending in preserved:
                self._send_unlocked(pending)
            self._send_unlocked(command)

    def wave(self, seconds: float = 2.5) -> None:
        self.send({"cmd": "WAVE", "seconds": seconds})

    def _run(self) -> None:
        if self.simulation:
            self._run_simulation()
            return
        if not self.token:
            self.on_telemetry(
                Telemetry(
                    connected=False,
                    last_error="Set ROBOT_WIFI_TOKEN before using the Wi-Fi ESP32 version.",
                )
            )
            return

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind((self.bind_host, self.telemetry_port))
            self._socket.settimeout(0.10)
            LOG.info("Waiting for authenticated ESP32 Wi-Fi telemetry on UDP %s", self.telemetry_port)
            last_heartbeat = 0.0
            reported_loss = False
            while not self._stop.is_set():
                try:
                    raw, source = self._socket.recvfrom(2048)
                except socket.timeout:
                    raw = b""
                    source = ("", 0)
                if raw:
                    self._handle_datagram(raw, source)
                    reported_loss = False

                now = time.monotonic()
                if self._endpoint and now - last_heartbeat >= 0.25:
                    self.send({"cmd": "HEARTBEAT"})
                    last_heartbeat = now
                self._flush_one()
                if self._last_seen and now - self._last_seen > 2.0 and not reported_loss:
                    reported_loss = True
                    self.on_telemetry(
                        Telemetry(
                            connected=False,
                            last_error="ESP32 Wi-Fi telemetry timed out.",
                        )
                    )
        except OSError as exc:
            LOG.exception("ESP32 Wi-Fi link failed")
            self.on_telemetry(Telemetry(connected=False, last_error=str(exc)))

    def _handle_datagram(self, raw: bytes, source: tuple[str, int]) -> None:
        try:
            payload = json.loads(raw.decode("utf-8"))
            if payload.get("type") != "telemetry" or payload.get("token") != self.token:
                return
            self._endpoint = source
            self._last_seen = time.monotonic()
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
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            LOG.warning("Bad Wi-Fi ESP32 packet from %s", source[0])

    def _flush_one(self) -> None:
        if not self._socket or not self._endpoint:
            return
        with self._tx_lock:
            try:
                message = self._tx.get_nowait()
            except queue.Empty:
                return
        packet = {**message, "token": self.token}
        try:
            self._socket.sendto(json.dumps(packet, separators=(",", ":")).encode("utf-8"), self._endpoint)
        except OSError as exc:
            LOG.warning("Could not send command to ESP32 Wi-Fi endpoint: %s", exc)

    def _run_simulation(self) -> None:
        LOG.warning("ROBOT_SIMULATION=true: Wi-Fi ESP32 commands will not move motors")
        while not self._stop.wait(0.25):
            while True:
                try:
                    message = self._tx.get_nowait()
                except queue.Empty:
                    break
                LOG.info("SIM Wi-Fi ESP32 <- %s", message)
            self.on_telemetry(Telemetry(connected=False, battery_v=12.0))
