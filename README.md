# College AI Museum Welcome Robot

A safety-first starter project for a laptop-controlled differential-drive welcome
robot. The laptop handles camera vision, VIP recognition, Sarvam speech and event
answers. The ESP32 handles motors, wheel encoders, ultrasonic sensors, arm servos,
dead reckoning, patrol and immediate stopping.

This is a prototype, not a finished public-space robot. Test it with the drive
wheels raised, then in a closed area with a spotter and physical emergency stop
before allowing it near visitors.

## What this version does

- It starts in **Idle**. Detection alone never moves the robot.
- **Welcome** enables YOLO person detection. The robot turns toward the largest
  person, approaches slowly, and stops based on image size or either front ultrasonic
  sensor.
- At greeting distance it stops, waves both arms, welcomes an ordinary visitor
  or uses a configured VIP greeting, and asks: “Can I help you to see our AI
  museum?”
- It records the answer, uses Sarvam Saaras v3 for STT, Sarvam-30B for the
  event-grounded answer, and Bulbul v3 for TTS.
- A “no” response ends the interaction and puts the robot into stationary Idle.
- **Patrol** makes a repeatable square loop using wheel-encoder dead reckoning.
- An obstacle causes a right-side rectangular detour, after which the robot
  returns to the original path and heading.
- **Stop** immediately clears queued motion commands.
- DroidCam supplies video from the phone over a preferred USB connection or a
  direct local-network stream.
- YOLO26n detects visitors; InsightFace SCRFD + ArcFace recognizes VIPs after
  three agreeing frames. Haar/LBPH remains an automatic diagnostic fallback.
- A phone-friendly web remote provides Welcome, Patrol and Stop buttons.
- A physical emergency-stop input and communication watchdog run on the ESP32.
- `ROBOT_SIMULATION=true` lets the entire laptop application run without motors.

## System architecture

```text
Phone browser (private hotspot / LAN)
        │  PIN-protected HTTP
        ▼
Laptop: FastAPI state machine
  ├─ DroidCam phone video → YOLO person detection
  │                         + SCRFD/ArcFace VIP matching
  ├─ microphone/speaker → Sarvam STT / LLM / TTS
  ├─ event.yaml → grounded prompt (unknown facts go to help desk)
  └─ USB serial JSON + heartbeat
        ▼
ESP32 safety controller
  ├─ motor driver → two geared drive motors
  ├─ quadrature encoders → dead-reckoned x, y, heading
  ├─ two front ultrasonic sensors → local motion stop / detour
  ├─ two servos → hand wave
  └─ normally-closed physical E-stop
```

## Required hardware

The two wheel encoders are **required** for dead reckoning; they were missing
from the original hardware list.

| Part | Requirement / note |
|---|---|
| Laptop | Windows 10/11, Python 3.11 recommended, USB ports, event Wi-Fi or a private hotspot |
| ESP32 development board | One board, connected to the laptop by a data-capable USB cable |
| Drive base | Two geared DC motors **with quadrature encoders**, wheels and a caster |
| Motor driver | Dual driver rated above each motor's measured **stall current**; avoid choosing by normal running current |
| Two ultrasonic sensors | HC-SR04 or equivalent; use a 5 V-to-3.3 V divider on every ESP32 ECHO input |
| Two arm servos | Use a separate regulated 5–6 V servo supply; connect its ground to ESP32 ground |
| Phone camera | Android/iPhone mounted rigidly in landscape orientation, running DroidCam |
| Microphone and speaker | Prefer separate USB devices; DroidCam audio is intentionally disabled |
| Power | Battery, suitable BMS/charger, fuse, master isolator, and DC converters for laptop/motors/servos |
| Physical E-stop | Normally-closed mushroom switch wired so opening it removes motor power; also connect a monitored contact to GPIO 21 |
| Recommended IMU | BNO085/BNO086 or similar to limit heading drift; the starter currently uses encoders only |
| Strongly recommended | Soft bumper ring and a human spotter during the event |

Do not power motors or servos from the ESP32 regulator. All grounds must be
common, but motor noise should be isolated with correct wiring, decoupling and
separate regulators.

## Software to install

1. Python 3.11 (64 bit) from the official Python installer.
2. Arduino IDE 2.x.
3. DroidCam on the phone and the current DroidCam Client on Windows 10/11.
4. Ollama for Windows, used to run the local Llama model on the RTX 4050.
5. In Arduino IDE, install the **esp32 by Espressif Systems** board package.
6. In Arduino Library Manager, install:
   - `ArduinoJson` by Benoit Blanchon
   - `ESP32Servo`
7. Create a Sarvam account and API subscription key.

The current Sarvam interfaces used here are the official `/speech-to-text` API
with `saaras:v3`, `/v1/chat/completions` with `sarvam-30b` as an LLM fallback,
and `/text-to-speech` with `bulbul:v3`.

## Laptop setup on Windows PowerShell

Run these commands from this directory:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Edit `.env`:

```dotenv
SARVAM_API_KEY=your_real_key
LLM_PROVIDER=local_first
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=llama3.2:3b-instruct-q4_K_M
OLLAMA_LOCAL_TIMEOUT_SECONDS=8
OLLAMA_CONTEXT_TOKENS=8192
LLM_MAX_OUTPUT_TOKENS=120
OLLAMA_KEEP_ALIVE=30m
ROBOT_SERIAL_PORT=COM5
ROBOT_SERIAL_BAUD=115200
# DroidCam virtual camera index discovered by scripts/list_cameras.py
CAMERA_SOURCE=1
CAMERA_WIDTH=1280
CAMERA_HEIGHT=720
CAMERA_FPS=30
VISION_BACKEND=deep
INSIGHTFACE_MODEL=buffalo_s
VIP_SIMILARITY_THRESHOLD=0.50
REMOTE_HOST=0.0.0.0
REMOTE_PORT=8000
REMOTE_CONTROL_PIN=change_this_pin
ROBOT_SIMULATION=true
```

Keep the Sarvam key only in `.env`; never put it in JavaScript, ESP32 firmware,
screenshots or Git.

## Install the local Llama model

Yes, the local model requires a one-time download.

1. Download and install Ollama for Windows from <https://ollama.com/download/windows>.
2. Open a new PowerShell window and download the selected 3B Q4 model:

```powershell
ollama pull llama3.2:3b-instruct-q4_K_M
```

The model download is approximately 2 GB. Verify it before starting the robot:

```powershell
ollama list
ollama run llama3.2:3b-instruct-q4_K_M "Answer in one short sentence: what is an AI museum?"
```

Do not download the 6.4 GB FP16 variant for this laptop; it would consume the
RTX 4050's VRAM and leave too little space for vision. The application warms the
Q4 model in the background, keeps it loaded for 30 minutes, and unloads it when
the robot server shuts down.

`LLM_PROVIDER` supports three modes:

- `local_first`: use local Llama and automatically fall back to Sarvam;
- `local_only`: never send LLM questions to Sarvam;
- `sarvam`: disable local Llama and always use Sarvam-30B.

Sarvam remains necessary for STT and TTS even when Llama answers locally.

## Configure DroidCam and deep vision

### Preferred: USB virtual camera

1. Install the current DroidCam Client on Windows and DroidCam on the phone.
2. Connect the phone using a good USB data cable. For Android, enable Developer
   Options and USB Debugging; approve the computer on the phone.
3. Add the phone in DroidCam Client and activate it.
4. Select 1280×720 and 30 FPS. Disable DroidCam audio.
5. Mount the phone rigidly in landscape orientation, near the robot's centerline.
6. With DroidCam active, discover its OpenCV index:

```powershell
.\.venv\Scripts\python.exe scripts\list_cameras.py
```

Put the working number into `CAMERA_SOURCE`. If the laptop camera is index `0`
and DroidCam is index `1`, use `CAMERA_SOURCE=1`.

### Alternative: direct Wi-Fi video

DroidCam also exposes a local video URL. Replace the phone IP with the address
shown by DroidCam:

```dotenv
CAMERA_SOURCE=http://192.168.1.20:4747/video/1280x720
```

USB is preferred because event Wi-Fi interference can introduce dropped frames
and latency. If Wi-Fi is unavoidable, reserve a static IP for the phone and use
a private 5 GHz hotspot. The direct HTTP feed is not encrypted, so never place
it on a public network. The controller automatically attempts to reconnect a
lost stream.

### First model download

With Internet available, start the application once before the event. YOLO26n
and the selected InsightFace pack are downloaded and cached during the first
start. Do not wait until event day. `buffalo_s` is the balanced CPU-laptop
default; test `buffalo_l` only if the laptop sustains acceptable frame rate.

The Ultralytics components are AGPL-3.0 by default, and InsightFace code and
pretrained weights have separate licensing terms. Confirm that your college's
use and source-release plan satisfy both before deployment.

## Configure event and VIP data

Edit [`config/event.yaml`](config/event.yaml) with event-level details, schedule,
facilities, help desk, emergency information and authoritative corrections. The
complete 17-project catalog is stored separately in
[`config/project_catalog.json`](config/project_catalog.json).

Fifteen catalog projects currently contain blank or placeholder zones. The
loader clears “leave these” placeholders so the robot directs unresolved
location questions to the help desk instead of speaking placeholder text. It
also normalizes the supplied `oAicial_name` typo for the object-detection
project without changing the imported JSON file.

For each question, the laptop sends Sarvam the event information, a compact
index of all 17 project names, zones and purpose summaries, and up to four full
project records selected locally using names, aliases, keywords and question
terms. This avoids sending the entire 126 KB catalog on every request while
retaining a useful fallback for multilingual questions.

While the server is running, call this endpoint after saving either configuration
file to reload both without restarting:

```powershell
$body = @{ pin = "your_control_pin" } | ConvertTo-Json
Invoke-RestMethod -Method Post -ContentType "application/json" -Body $body http://127.0.0.1:8000/api/reload-event
```

For each VIP:

1. Add a record to [`config/vips.yaml`](config/vips.yaml).
2. Put 3–8 clear, consented, front-facing photos per person in `data/vips/`.
3. List each filename under that VIP's `photos`.
4. Restart the laptop application to retrain the recognizer.
5. Validate the person and several non-VIPs in the real event lighting.

Deep recognition averages the configured VIP photo embeddings and requires
three consecutive agreeing frames before using a name. Its threshold still must
be calibrated in the real venue. A false match must never unlock a door,
payment, private data or safety-sensitive action. Obtain consent and delete the
photos after the event according to your college privacy policy.

## ESP32 setup and wiring

Open:

```text
firmware/welcome_robot_esp32/welcome_robot_esp32.ino
```

The pin constants are at the top of the sketch. The sample pin map is:

| Function | GPIO |
|---|---:|
| Left motor PWM / IN1 / IN2 | 25 / 26 / 27 |
| Right motor PWM / IN1 / IN2 | 33 / 32 / 4 |
| Left encoder A / B | 34 / 35 |
| Right encoder A / B | 36 / 39 |
| Left ultrasonic TRIG / ECHO | 16 / 17 |
| Right ultrasonic TRIG / ECHO | 18 / 19 |
| Left / right arm servo | 13 / 14 |
| Monitored normally-closed E-stop | 21 |

Before uploading, measure and edit:

```cpp
WHEEL_DIAMETER_M
TRACK_WIDTH_M
ENCODER_COUNTS_PER_WHEEL_REV
LEFT_ENCODER_REVERSED
RIGHT_ENCODER_REVERSED
LEFT_MOTOR_REVERSED
RIGHT_MOTOR_REVERSED
```

Upload the sketch with the correct ESP32 board and COM port selected. With a
normally-closed E-stop, GPIO 21 is LOW in the safe/run position and becomes HIGH
if the switch is pressed, disconnected or a wire breaks.

## First run in simulation

Leave `ROBOT_SIMULATION=true`, then run:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

On the laptop open:

```text
http://127.0.0.1:8000
```

Use `ipconfig` to find the laptop's Wi-Fi IPv4 address. Connect the phone to the
same private network and open, for example:

```text
http://192.168.137.1:8000
```

Allow Python through Windows Firewall only on **Private networks**. Use the PIN
from `.env`. Do not expose this control server to public Wi-Fi or the Internet.

## Run with the robot

1. Put the robot on blocks so its wheels cannot touch the floor.
2. Turn on the physical E-stop/master-power safety system.
3. Confirm the ESP32 serial port in Windows Device Manager.
4. Set `ROBOT_SIMULATION=false` in `.env`.
5. Start the same Uvicorn command.
6. Confirm the remote says ESP32 `Connected`.
7. Test Stop first, then wheel direction, encoder direction and ultrasonic stop.
8. Only then move to a closed floor-test area with a spotter.

Useful checks:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app tests
```

## Calibration and acceptance plan

### 1. Bench safety

- Wheels raised; arm linkages disconnected for the first servo test.
- Pressing the physical E-stop removes motor power.
- Unplugging USB or killing the laptop stops the motors in about 1.2 seconds.
- Remote Stop halts both motors immediately.
- Either ultrasonic sensor prevents forward approach below the configured limit.

### 2. Dead-reckoning calibration

- Command ten wheel revolutions and correct encoder counts per revolution.
- Drive a measured 2 m line and correct wheel diameter until distance error is
  below 3%.
- Turn ten full rotations and correct track width until average heading error is
  below 3 degrees per turn.
- Repeat on the actual event floor with the final payload and tire pressure.

Wheel slip, uneven floors and caster drag accumulate error. Add IMU fusion and
periodic visual landmarks (AprilTags) if the patrol must remain aligned for
hours.

### 3. Welcome test

- Begin at 0.16 m/s maximum with a spotter holding the E-stop.
- Test one person, side-on faces, several people and a person crossing the path.
- Measure the actual stop distance. Tune `APPROACH_PERSON_HEIGHT`,
  `MIN_OBSTACLE_CM` and ESP32 `OBSTACLE_STOP_CM`.
- Test speech with event noise, Indian English and the required local languages.
- Confirm unknown questions are directed to the help desk, not invented.

### 4. Patrol and obstacle test

- Mark a 2 m square in a closed area.
- Run 20 loops and record final position/heading drift.
- Place boxes at different points and verify the rectangular detour returns to
  the marked line.
- Test soft, black, angled and narrow obstacles; ultrasonic sensors can miss
  clothing and angled surfaces.
- Never use patrol among visitors until the event route is barrier-separated or
  a better safety sensor stack is fitted.

### 5. Event-day checklist

- Full battery and spare power; tested fuse and E-stop.
- Private hotspot, remote PIN changed, Sarvam credits and Internet verified.
- Local fallback greeter/volunteer available if Internet fails.
- Camera/privacy notice displayed and VIP consent recorded.
- Clear patrol lane, speed limited, dedicated operator watching the robot.
- Test Stop before doors open and after every power cycle.

## Important limitations and decisions to discuss

1. **YOLO and two ultrasonic sensors are not enough for safe autonomous crowd
   navigation.** They have blind zones and can miss fabric, glass, thin objects
   and angled surfaces. A safety lidar/depth camera, bumper switches, lower speed
   and supervised/segregated route are recommended.
2. **Dead reckoning drifts.** Encoders are mandatory; an IMU and AprilTag
   relocalization are recommended. The current “return to path” is approximate.
3. **“Return to ideal position” currently means stop and enter Idle.** Driving
   autonomously back to a home point through visitors would require mapping,
   localization and path planning. It is intentionally not attempted here.
4. **Face recognition can make mistakes.** Use multiple consented images,
   three-frame confirmation and a conservative cosine-similarity threshold.
   Keep VIP greetings respectful even if recognition fails; never announce
   sensitive details.
5. **Speech depends on Internet and Sarvam availability/credits.** Event noise
   will reduce STT accuracy. Use a directional microphone and retain a human help
   desk.
6. **Motor control is initially open-loop.** Encoders calculate odometry, but
   adding per-wheel speed PID will make straight driving and turns more accurate.
7. **The obstacle detour assumes space on the robot's right.** Two forward
   sensors cannot prove the side is clear. Side sensors or lidar are needed
   before enabling this around people.
8. **No autonomous mode is risk-free in a public event.** Use a physical E-stop,
   a trained operator, a low center of gravity, guards over wheels/gears, and a
   documented risk assessment approved by the college.

## Project layout

```text
app/
  main.py            FastAPI API and mobile remote
  controller.py      mode state machine and visitor interaction
  vision.py          DroidCam reconnect, YOLO visitors, ArcFace VIP matching
  speech.py          Sarvam STT, cloud LLM fallback and TTS
  llm.py             local Ollama Llama with automatic Sarvam fallback
  serial_link.py     laptop↔ESP32 JSON, watchdog heartbeat, simulation
  event_store.py     validated catalog loading and local project retrieval
config/
  event.yaml         editable event knowledge
  project_catalog.json  imported 17-project event catalog
  vips.yaml          VIP names, greetings and photo filenames
data/vips/           local VIP photos (ignored by Git)
firmware/
  welcome_robot_esp32/welcome_robot_esp32.ino
tests/
scripts/
  list_cameras.py    discover the DroidCam virtual camera index
```
