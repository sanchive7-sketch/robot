from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class RobotMode(str, Enum):
    IDLE = "idle"
    WELCOME = "welcome"
    MANUAL = "manual"
    STOPPED = "stopped"
    EMERGENCY = "emergency"


@dataclass
class Telemetry:
    x_m: float = 0.0
    y_m: float = 0.0
    heading_rad: float = 0.0
    left_distance_cm: float = 999.0
    right_distance_cm: float = 999.0
    battery_v: float = 0.0
    connected: bool = False
    last_error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Visitor:
    center_x: float
    center_y: float
    proximity_fraction: float
    # The selected person's position is used for navigation, while this count
    # lets the interaction greet a group without needing face identification.
    person_count: int = 1
