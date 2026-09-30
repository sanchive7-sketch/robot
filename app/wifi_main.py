"""Wi-Fi ESP32 dashboard entry point.

Run ``uvicorn app.wifi_main:app``.  The normal ``app.main`` serial version is
intentionally left untouched.
"""

from . import serial_link
from .wifi_link import WifiLink

# ``main`` imports this symbol while it initialises the controller.  Replacing
# it before importing main reuses the exact same dashboard and safety logic.
serial_link.SerialLink = WifiLink

from .main import app  # noqa: E402,F401
