# Manual Wi-Fi robot with no ultrasonic sensors

Use this version only when you intentionally want manual motor control and no
ultrasonic obstacle detection.

## What this version does

- Uses Wi-Fi between the laptop dashboard and ESP32; it does not use COM3.
- Supports Manual mode, forward/back/left/right controls, Stop, and servo wave.
- Does not use ultrasonic sensors or wheel encoders.
- Rejects autonomous Welcome movement. The ESP32 will not drive automatically.
- Stops motors when manual direction messages stop for 0.5 seconds or the Wi-Fi
  heartbeat stops for about 1.2 seconds.

## Required safety equipment

This sketch requires a physical, normally-closed E-stop:

```text
ESP32 GPIO 21 -> normally-closed E-stop auxiliary contact -> GND
Battery positive -> fuse -> E-stop power contact -> motor-driver positive
```

Without the GPIO 21 contact connected to GND, it stays in emergency-stop mode.
Do not bypass this protection for floor testing.

## Use it

1. Open `firmware/welcome_robot_esp32_wifi_manual` in Arduino IDE.
2. Copy `wifi_secrets.h.example` to `wifi_secrets.h` and set the Wi-Fi values.
3. Upload `welcome_robot_esp32_wifi_manual.ino`.
4. Follow the laptop environment and startup steps in `WIFI_ESP32_VERSION.md`.
   It uses the same `app.wifi_main:app` dashboard and `ROBOT_WIFI_TOKEN`.
5. Test wheels raised. Select **Manual**, hold a direction, and confirm release
   stops motors before any floor movement.

The laptop dashboard will show ultrasonic distance as `999 cm`, meaning that no
distance sensor is connected. Do not use Welcome or any autonomous mode.
