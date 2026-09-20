# Encoder-Free Robot Wiring Guide

This guide matches the GPIO assignments in
`firmware/welcome_robot_esp32/welcome_robot_esp32.ino`.

## Components currently available

- ESP32 development board
- DC motors and wheels
- L298N dual-channel motor-driver module with `ENA`, `ENB` and `IN1`–`IN4`
- One ultrasonic sensor (assumed to be HC-SR04-compatible)
- Two hobby servos (assumed from the current firmware)
- Buck converter
- 2 ohm and 3 ohm resistors

## Stop: important electrical checks

1. **Do not use actual 2 ohm and 3 ohm resistors on the ultrasonic ECHO
   connection.** They are far too small. The required values are approximately
   **2 kilo-ohms (2 kΩ)** and **3 kilo-ohms (3 kΩ)**, or a proper 5 V-to-3.3 V
   logic-level converter.
2. The ESP32 GPIO limit is 3.6 V. Never connect a 5 V ECHO output directly to
   an ESP32 GPIO.
3. A battery or other motor power source is required but was not included in
   the component list. Its voltage must match the motors, motor driver and buck
   converter input ratings.
4. L298N modules differ in their onboard 5 V regulator and jumper arrangement.
   Check the labels printed on your module before wiring its `5V` terminal.
5. Disconnect motor power while changing wires. Perform the first test with
   every wheel raised off the floor.

## System wiring overview

```text
Battery / motor power source
  |
  +-- fuse + main switch/E-stop --> motor-driver motor-power input
  |
  `-- buck-converter input
         `-- regulated 5 V output --> servos + HC-SR04

Laptop USB --> ESP32 USB connector (recommended for initial testing)

All grounds connected together:
  battery negative
  motor-driver GND
  buck input/output GND
  ESP32 GND
  ultrasonic GND
  both servo GND wires
```

Do not run motor current through a solderless breadboard. Use suitably sized
wires and terminals. Measure the buck converter output with a multimeter before
connecting the servos or ultrasonic sensor.

## ESP32 pin summary

| Purpose | ESP32 GPIO | Connects to |
|---|---:|---|
| Left motor speed | 25 | Driver channel A PWM/EN |
| Left motor direction 1 | 26 | Driver channel A IN1 |
| Left motor direction 2 | 27 | Driver channel A IN2 |
| Right motor speed | 33 | Driver channel B PWM/EN |
| Right motor direction 1 | 32 | Driver channel B IN1 |
| Right motor direction 2 | 4 | Driver channel B IN2 |
| Ultrasonic TRIG | 16 | HC-SR04 TRIG |
| Ultrasonic ECHO | 17 | HC-SR04 ECHO through level conversion |
| Left servo signal | 13 | Left servo signal wire |
| Right servo signal | 14 | Right servo signal wire |
| E-stop sense | 21 | Normally-closed contact to GND |
| USB serial | USB connector | Laptop USB cable |

GPIO 18 and GPIO 19 are reserved by the current firmware for an optional
second ultrasonic sensor. Leave them unconnected when using one sensor.

## L298N motor-driver and DC-motor wiring

The firmware treats the chassis as differential drive: one motor channel for
the left wheel(s), and one for the right wheel(s).

Remove the L298N module's `ENA` and `ENB` jumper caps. The ESP32 must drive
these pins with PWM for speed control.

| L298N terminal | ESP32 or motor connection | Purpose |
|---|---|---|
| `ENA` | GPIO25 | Left-channel speed/PWM |
| `IN1` | GPIO26 | Left-channel direction input 1 |
| `IN2` | GPIO27 | Left-channel direction input 2 |
| `OUT1`, `OUT2` | Left DC motor | Left motor power output |
| `ENB` | GPIO33 | Right-channel speed/PWM |
| `IN3` | GPIO32 | Right-channel direction input 1 |
| `IN4` | GPIO4 | Right-channel direction input 2 |
| `OUT3`, `OUT4` | Right DC motor | Right motor power output |
| `GND` | ESP32 GND and battery negative | Common ground |

```text
ESP32                       L298N
GPIO25 -------------------- ENA       OUT1/OUT2 ---- left motor
GPIO26 -------------------- IN1
GPIO27 -------------------- IN2

GPIO33 -------------------- ENB       OUT3/OUT4 ---- right motor
GPIO32 -------------------- IN3
GPIO4  -------------------- IN4

ESP32 GND ----------------- GND ------ battery negative
```

If there are four motors, the two left motors form the left side and the two
right motors form the right side. Do not connect two motors in parallel until
the motor driver's **per-channel continuous and stall-current ratings** have
been checked.

### L298N power terminals

| L298N power terminal | Connection |
|---|---|
| `+12V`, `VS`, or motor-power input | Battery positive through fuse and main switch/E-stop |
| `GND` | Battery negative and the common GND bus |
| `5V` logic terminal | Follow the module's regulator-jumper instructions below |

The terminal printed `+12V` is the motor-supply input on common modules; that
label does **not** mean every motor should receive 12 V. The power-source
voltage must match the motor rating and remain inside the module rating.

Many L298N modules have a `5V-EN` or regulator jumper:

- With that jumper fitted, the onboard regulator may drive the module's `5V`
  terminal as an output, but only within the module maker's permitted motor
  supply range.
- With that jumper removed, the module may require regulated 5 V on its `5V`
  logic terminal.
- Do not connect an external 5 V supply to a terminal that is currently acting
  as the onboard regulator's 5 V output.
- Never connect the L298N `5V` terminal to the ESP32 `3V3` pin.

Because module layouts vary, verify the `5V-EN` jumper and terminal printing on
your exact board before applying motor power. The ESP32 can remain powered by
USB during initial tests.

If Forward turns or runs backward during the raised-wheel test, first switch
off motor power. Swap the two wires of the incorrectly rotating motor, or
calibrate the firmware's motor-reversal constants.

## Servo wiring

Do not power the servos from the ESP32 3.3 V pin.

| Servo wire | Connection |
|---|---|
| Red | Regulated supply matching the servo rating, commonly 5–6 V |
| Brown/black | Common GND |
| Yellow/orange/white | Signal GPIO |

| Servo | Signal GPIO |
|---|---:|
| Left arm | 13 |
| Right arm | 14 |

The buck converter must support the combined **stall current** of both servos,
not only their no-load current. A 470–1000 µF capacitor near the servo supply
can help with short voltage dips, but it does not replace a correctly rated
power supply.

## One ultrasonic sensor

Mount the single sensor near the centre front of the robot.

| HC-SR04 pin | Connection |
|---|---|
| VCC | Regulated 5 V |
| GND | Common GND |
| TRIG | ESP32 GPIO16 |
| ECHO | Voltage divider/level converter, then GPIO17 |

### Safe ECHO voltage divider

Use resistor values in **kilo-ohms**, not ohms:

```text
HC-SR04 ECHO ---- 2 kΩ ----+---- ESP32 GPIO17
                            |
                           3 kΩ
                            |
                           GND
```

This produces approximately 3.0 V from a 5 V ECHO signal:

```text
5 V × 3 kΩ / (2 kΩ + 3 kΩ) = 3.0 V
```

Actual 2 Ω and 3 Ω resistors would draw approximately 1 ampere from a 5 V
signal in the ideal calculation and can damage the sensor or wiring. **Do not
connect ECHO until you have kΩ resistors or a level converter.**

The firmware currently polls a second sensor on GPIO18/GPIO19. With that sensor
absent, it normally times out as no obstacle, but a floating input can produce
unreliable readings. For dependable operation, the firmware should eventually
be configured explicitly for one sensor or a second sensor should be fitted.

## Physical emergency stop

The firmware configures GPIO21 with an internal pull-up and expects a
normally-closed contact to GND:

```text
ESP32 GPIO21 --> normally-closed E-stop auxiliary contact --> GND
```

If GPIO21 is left open, the firmware reads HIGH and remains in Emergency mode,
so the motors will not run.

For a raised-wheel bench test only, GPIO21 can be temporarily jumpered directly
to GND. This disables that software safety input. Do not place the robot on the
floor in that configuration. The final robot should also have an E-stop or
master switch that physically disconnects motor-driver power.

## No wheel encoders

Leave GPIO34, GPIO35, GPIO36 and GPIO39 unconnected. They are the encoder pins
in the existing firmware. ESP32 GPIO34–39 are input-only and do not have
internal pull resistors, so disconnected inputs can pick up noise.

- **Manual Control:** does not use encoder position and can be tested with the
  precautions in this guide.
- **Welcome conversation while stationary:** camera detection, greeting,
  microphone and answers can still work.
- **Automatic Welcome movement and return-home:** do not use without encoders
  or another positioning system. The fixed movement boundary cannot be trusted.

## Recommended connection order

1. Photograph both sides of the L298N module and identify its `5V-EN`, `ENA`
   and `ENB` jumpers and power-terminal labels.
2. Obtain a correctly rated battery/power source, fuse, switch and E-stop.
3. With everything disconnected, set the buck output to the required servo and
   sensor voltage using a multimeter.
4. Join all grounds.
5. Connect the ESP32 to the driver **signal inputs only**.
6. Connect the motor outputs with motor power still switched off.
7. Connect servo grounds and signal wires, then their regulated supply.
8. Connect ultrasonic VCC, GND and TRIG. Connect ECHO only through a safe level
   converter or 2 kΩ/3 kΩ divider.
9. Connect the GPIO21 normally-closed E-stop input.
10. Raise the wheels, connect ESP32 USB, then apply motor power.
11. Test the physical E-stop and dashboard STOP before testing directions.
12. Test Forward, Backward, Left and Right briefly at low speed.
13. Test front obstacle stopping before placing the robot on the floor.

## Information still needed for final verification

- Clear photo of both sides of the L298N module and its jumper positions
- Motor rated voltage and stall current
- Battery/power-source voltage and current rating
- Buck-converter model and maximum output current
- Ultrasonic sensor model printed on the board
- Servo model numbers
- Whether the resistors are marked 2 Ω/3 Ω or 2 kΩ/3 kΩ

Do not apply motor power until these ratings are compatible.

## Reference documents

- Espressif states that ESP GPIO tolerance is 3.6 V and higher voltages require
  a divider: <https://docs.espressif.com/projects/esp-faq/en/latest/hardware-related/hardware-design.html>
- Espressif documents that ESP32 GPIO34–39 are input-only and have no internal
  pull-up/pull-down resistors: <https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/gpio.html>
- HC-SR04 reference data specifies a 5 V working voltage:
  <https://www.digikey.com/htmldatasheets/production/1979760/0/0/1/hc-sr04.html>
- STMicroelectronics documents the L298 as a dual full-bridge driver with
  independent Enable A and Enable B inputs:
  <https://www.st.com/en/motor-drivers/l298.html>
