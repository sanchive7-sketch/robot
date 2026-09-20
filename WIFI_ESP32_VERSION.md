# Wi-Fi ESP32 version

This is a separate version of the robot controller. It keeps the existing USB
firmware and serial dashboard unchanged.

```text
Phone camera + microphone -> laptop (DroidCam / audio)
Laptop -> detection, speech, Ollama, dashboard
Laptop <-> private Wi-Fi UDP <-> ESP32 -> motors, sensors, servos
```

The ESP32 does **not** run camera detection or speech. It only receives motor
commands and returns sensor/odometry telemetry. This requires the laptop and
ESP32 to be on the same private Wi-Fi router. The ESP32 can join **2.4 GHz
Wi-Fi only**. Do not use public Wi-Fi or a phone hotspot with client isolation
enabled.

## Files added

| File | Purpose |
|---|---|
| `firmware/welcome_robot_esp32_wifi/welcome_robot_esp32_wifi.ino` | Wi-Fi upload sketch |
| `app/wifi_main.py` | Wi-Fi dashboard entry point |
| `app/wifi_link.py` | Authenticated UDP laptop-to-ESP32 transport |

The original `firmware/welcome_robot_esp32/welcome_robot_esp32.ino` is not
changed.

## Configure the ESP32

1. In the Wi-Fi sketch folder, copy `wifi_secrets.h.example` to
   `wifi_secrets.h`.
2. Set `WIFI_SSID`, `WIFI_PASSWORD`, and `WIFI_SHARED_TOKEN`.
3. Upload `welcome_robot_esp32_wifi.ino` using **ESP32 Dev Module**.

`wifi_secrets.h` is ignored by Git so your password and token are not staged.

## Configure and start the laptop

Add these values to the laptop `.env`; the token must exactly match the one in
`wifi_secrets.h`:

```dotenv
ROBOT_SIMULATION=false
ROBOT_WIFI_TOKEN=replace_with_the_same_long_random_token
ROBOT_WIFI_BIND_HOST=0.0.0.0
ROBOT_WIFI_TELEMETRY_PORT=9011
```

Start the Wi-Fi dashboard, not the normal USB/serial dashboard:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.wifi_main:app --host 0.0.0.0 --port 8000
```

The terminal first says it is waiting for Wi-Fi telemetry. Once the ESP32 is
connected to Wi-Fi, its telemetry identifies its IP automatically and the
dashboard shows the ESP32 as connected.

If Windows Firewall asks, allow Python on **Private networks**. If it does not
ask, run PowerShell as Administrator once to permit the ESP32 telemetry packet:

```powershell
New-NetFirewallRule -DisplayName "Welcome Robot ESP32 Wi-Fi telemetry" -Direction Inbound -Protocol UDP -LocalPort 9011 -Action Allow -Profile Private
```

## Safety behaviour

- UDP commands carry the shared token; packets with the wrong token are
  ignored.
- If Wi-Fi commands stop arriving, the existing ESP32 `LINK_TIMEOUT_MS` safety
  check stops motors after about 1.2 seconds.
- The existing manual-control lease still stops movement after about 0.5
  seconds when the operator releases the control.
- Keep the physical E-stop. Wi-Fi is not a replacement for it.
- Test with wheels raised before floor movement.
