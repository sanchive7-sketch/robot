/*
 * College Welcome Robot - ESP32 safety and motion controller
 *
 * Libraries (Arduino Library Manager):
 *   ArduinoJson by Benoit Blanchon
 *   ESP32Servo by Kevin Harrington / John K. Bennett
 *
 * IMPORTANT: verify every pin, motor direction, encoder CPR, wheel size, track
 * width and ultrasonic voltage level on your chassis before lifting the wheels
 * off the bench. HC-SR04 ECHO is 5 V; use a resistor divider to protect ESP32.
 */
#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>

// ----- Pin map: change to match the actual wiring -----
constexpr uint8_t LEFT_PWM = 25;
constexpr uint8_t LEFT_IN1 = 26;
constexpr uint8_t LEFT_IN2 = 27;
constexpr uint8_t RIGHT_PWM = 33;
constexpr uint8_t RIGHT_IN1 = 32;
constexpr uint8_t RIGHT_IN2 = 4;

constexpr uint8_t LEFT_ENC_A = 34;
constexpr uint8_t LEFT_ENC_B = 35;
constexpr uint8_t RIGHT_ENC_A = 36;
constexpr uint8_t RIGHT_ENC_B = 39;

constexpr uint8_t US_LEFT_TRIG = 16;
constexpr uint8_t US_LEFT_ECHO = 17;
constexpr uint8_t US_RIGHT_TRIG = 18;
constexpr uint8_t US_RIGHT_ECHO = 19;
constexpr uint8_t LEFT_ARM_SERVO = 13;
constexpr uint8_t RIGHT_ARM_SERVO = 14;
constexpr uint8_t ESTOP_PIN = 21;  // Normally-closed switch to GND.

// ----- Chassis calibration: MEASURE these values -----
constexpr float WHEEL_DIAMETER_M = 0.100f;
constexpr float TRACK_WIDTH_M = 0.315f;
constexpr int32_t ENCODER_COUNTS_PER_WHEEL_REV = 600;
constexpr bool LEFT_ENCODER_REVERSED = false;
constexpr bool RIGHT_ENCODER_REVERSED = true;
constexpr bool LEFT_MOTOR_REVERSED = false;
constexpr bool RIGHT_MOTOR_REVERSED = true;

constexpr float METERS_PER_TICK =
    (PI * WHEEL_DIAMETER_M) / ENCODER_COUNTS_PER_WHEEL_REV;
constexpr float OBSTACLE_STOP_CM = 42.0f;
constexpr float PATROL_LEG_M = 2.0f;
constexpr float AVOID_OFFSET_M = 0.45f;
constexpr float AVOID_FORWARD_M = 0.75f;
constexpr uint32_t LINK_TIMEOUT_MS = 1200;
constexpr uint32_t TELEMETRY_PERIOD_MS = 200;

enum class Mode { STOPPED, WELCOME, PATROL, EMERGENCY };
enum class PatrolState {
  DRIVE_LEG,
  TURN_CORNER,
  AVOID_TURN_RIGHT,
  AVOID_OFFSET_OUT,
  AVOID_TURN_FORWARD,
  AVOID_PASS,
  AVOID_TURN_BACK,
  AVOID_OFFSET_IN,
  AVOID_TURN_RESUME
};

volatile int32_t leftTicks = 0;
volatile int32_t rightTicks = 0;
int32_t previousLeftTicks = 0;
int32_t previousRightTicks = 0;

Mode mode = Mode::STOPPED;
PatrolState patrolState = PatrolState::DRIVE_LEG;
Servo leftArm;
Servo rightArm;

float poseX = 0.0f;
float poseY = 0.0f;
float poseHeading = 0.0f;
float leftDistanceCm = 999.0f;
float rightDistanceCm = 999.0f;
float stateStartX = 0.0f;
float stateStartY = 0.0f;
float targetHeading = 0.0f;
float patrolHeading = 0.0f;
float patrolLegTargetM = PATROL_LEG_M;
float avoidanceRemainingM = PATROL_LEG_M;
float commandedLinear = 0.0f;
float commandedAngular = 0.0f;
uint32_t lastLinkMs = 0;
uint32_t lastTelemetryMs = 0;
uint32_t waveUntilMs = 0;
bool wavePhase = false;

void IRAM_ATTR onLeftEncoder() {
  int direction = digitalRead(LEFT_ENC_A) == digitalRead(LEFT_ENC_B) ? 1 : -1;
  leftTicks += LEFT_ENCODER_REVERSED ? -direction : direction;
}

void IRAM_ATTR onRightEncoder() {
  int direction = digitalRead(RIGHT_ENC_A) == digitalRead(RIGHT_ENC_B) ? 1 : -1;
  rightTicks += RIGHT_ENCODER_REVERSED ? -direction : direction;
}

float wrapAngle(float angle) {
  while (angle > PI) angle -= 2.0f * PI;
  while (angle < -PI) angle += 2.0f * PI;
  return angle;
}

float distanceFromStateStart() {
  const float dx = poseX - stateStartX;
  const float dy = poseY - stateStartY;
  return sqrtf(dx * dx + dy * dy);
}

float progressAlongHeading(float heading) {
  const float dx = poseX - stateStartX;
  const float dy = poseY - stateStartY;
  return dx * cosf(heading) + dy * sinf(heading);
}

void beginDistanceState() {
  stateStartX = poseX;
  stateStartY = poseY;
}

void setMotor(uint8_t pwmPin, uint8_t in1, uint8_t in2, int pwm, bool reversed) {
  pwm = constrain(pwm, -255, 255);
  if (reversed) pwm = -pwm;
  if (pwm == 0) {
    digitalWrite(in1, LOW);
    digitalWrite(in2, LOW);
    analogWrite(pwmPin, 0);
  } else {
    digitalWrite(in1, pwm > 0 ? HIGH : LOW);
    digitalWrite(in2, pwm > 0 ? LOW : HIGH);
    analogWrite(pwmPin, abs(pwm));
  }
}

void driveRobot(float linearMps, float angularRps) {
  commandedLinear = linearMps;
  commandedAngular = angularRps;
  // Open-loop velocity-to-PWM mapping. Replace with wheel-speed PID after
  // measuring the chassis. Odometry still comes from the encoders.
  const float leftMps = linearMps - angularRps * TRACK_WIDTH_M * 0.5f;
  const float rightMps = linearMps + angularRps * TRACK_WIDTH_M * 0.5f;
  constexpr float MAX_WHEEL_MPS = 0.45f;
  const int leftPwm = (int)(255.0f * leftMps / MAX_WHEEL_MPS);
  const int rightPwm = (int)(255.0f * rightMps / MAX_WHEEL_MPS);
  setMotor(LEFT_PWM, LEFT_IN1, LEFT_IN2, leftPwm, LEFT_MOTOR_REVERSED);
  setMotor(RIGHT_PWM, RIGHT_IN1, RIGHT_IN2, rightPwm, RIGHT_MOTOR_REVERSED);
}

void stopMotors() {
  driveRobot(0.0f, 0.0f);
}

float readUltrasonicCm(uint8_t trig, uint8_t echo) {
  digitalWrite(trig, LOW);
  delayMicroseconds(2);
  digitalWrite(trig, HIGH);
  delayMicroseconds(10);
  digitalWrite(trig, LOW);
  const uint32_t duration = pulseIn(echo, HIGH, 18000);
  if (duration == 0) return 999.0f;
  return duration * 0.0343f * 0.5f;
}

void updateOdometry() {
  noInterrupts();
  const int32_t currentLeft = leftTicks;
  const int32_t currentRight = rightTicks;
  interrupts();
  const int32_t deltaLeftTicks = currentLeft - previousLeftTicks;
  const int32_t deltaRightTicks = currentRight - previousRightTicks;
  previousLeftTicks = currentLeft;
  previousRightTicks = currentRight;

  const float dl = deltaLeftTicks * METERS_PER_TICK;
  const float dr = deltaRightTicks * METERS_PER_TICK;
  const float center = (dl + dr) * 0.5f;
  const float deltaHeading = (dr - dl) / TRACK_WIDTH_M;
  const float midHeading = poseHeading + deltaHeading * 0.5f;
  poseX += center * cosf(midHeading);
  poseY += center * sinf(midHeading);
  poseHeading = wrapAngle(poseHeading + deltaHeading);
}

bool turnToward(float heading) {
  const float error = wrapAngle(heading - poseHeading);
  if (fabsf(error) < 0.07f) {
    stopMotors();
    return true;
  }
  const float rate = constrain(error * 1.4f, -0.65f, 0.65f);
  driveRobot(0.0f, rate);
  return false;
}

void startPatrol() {
  patrolState = PatrolState::DRIVE_LEG;
  patrolHeading = poseHeading;
  patrolLegTargetM = PATROL_LEG_M;
  beginDistanceState();
}

void beginAvoidance() {
  const float progress = max(0.0f, progressAlongHeading(patrolHeading));
  avoidanceRemainingM =
      max(0.0f, patrolLegTargetM - progress - AVOID_FORWARD_M);
  targetHeading = wrapAngle(patrolHeading - PI * 0.5f);
  patrolState = PatrolState::AVOID_TURN_RIGHT;
  stopMotors();
}

void updatePatrol() {
  const float nearest = min(leftDistanceCm, rightDistanceCm);
  if (patrolState == PatrolState::DRIVE_LEG && nearest < OBSTACLE_STOP_CM) {
    beginAvoidance();
  }
  const bool avoidanceDrive =
      patrolState == PatrolState::AVOID_OFFSET_OUT ||
      patrolState == PatrolState::AVOID_PASS ||
      patrolState == PatrolState::AVOID_OFFSET_IN;
  if (avoidanceDrive && nearest < 25.0f) {
    stopMotors();  // Wait in place; do not advance the avoidance state.
    return;
  }

  switch (patrolState) {
    case PatrolState::DRIVE_LEG:
      if (progressAlongHeading(patrolHeading) >= patrolLegTargetM) {
        targetHeading = wrapAngle(patrolHeading + PI * 0.5f);
        patrolState = PatrolState::TURN_CORNER;
        stopMotors();
      } else {
        const float headingError = wrapAngle(patrolHeading - poseHeading);
        driveRobot(0.20f, constrain(headingError * 1.2f, -0.35f, 0.35f));
      }
      break;
    case PatrolState::TURN_CORNER:
      if (turnToward(targetHeading)) {
        patrolHeading = targetHeading;
        patrolLegTargetM = PATROL_LEG_M;
        beginDistanceState();
        patrolState = PatrolState::DRIVE_LEG;
      }
      break;
    case PatrolState::AVOID_TURN_RIGHT:
      if (turnToward(targetHeading)) {
        beginDistanceState();
        patrolState = PatrolState::AVOID_OFFSET_OUT;
      }
      break;
    case PatrolState::AVOID_OFFSET_OUT:
      if (distanceFromStateStart() >= AVOID_OFFSET_M) {
        targetHeading = patrolHeading;
        patrolState = PatrolState::AVOID_TURN_FORWARD;
        stopMotors();
      } else {
        driveRobot(0.14f, 0.0f);
      }
      break;
    case PatrolState::AVOID_TURN_FORWARD:
      if (turnToward(targetHeading)) {
        beginDistanceState();
        patrolState = PatrolState::AVOID_PASS;
      }
      break;
    case PatrolState::AVOID_PASS:
      if (distanceFromStateStart() >= AVOID_FORWARD_M) {
        targetHeading = wrapAngle(patrolHeading + PI * 0.5f);
        patrolState = PatrolState::AVOID_TURN_BACK;
        stopMotors();
      } else {
        driveRobot(0.14f, 0.0f);
      }
      break;
    case PatrolState::AVOID_TURN_BACK:
      if (turnToward(targetHeading)) {
        beginDistanceState();
        patrolState = PatrolState::AVOID_OFFSET_IN;
      }
      break;
    case PatrolState::AVOID_OFFSET_IN:
      if (distanceFromStateStart() >= AVOID_OFFSET_M) {
        targetHeading = patrolHeading;
        patrolState = PatrolState::AVOID_TURN_RESUME;
        stopMotors();
      } else {
        driveRobot(0.14f, 0.0f);
      }
      break;
    case PatrolState::AVOID_TURN_RESUME:
      if (turnToward(targetHeading)) {
        patrolLegTargetM = avoidanceRemainingM;
        beginDistanceState();
        patrolState = PatrolState::DRIVE_LEG;
      }
      break;
  }
}

void updateArms() {
  if (millis() >= waveUntilMs) {
    leftArm.write(90);
    rightArm.write(90);
    return;
  }
  static uint32_t lastPhaseMs = 0;
  if (millis() - lastPhaseMs > 260) {
    lastPhaseMs = millis();
    wavePhase = !wavePhase;
    leftArm.write(wavePhase ? 35 : 145);
    rightArm.write(wavePhase ? 145 : 35);
  }
}

void sendTelemetry() {
  JsonDocument doc;
  doc["type"] = "telemetry";
  doc["x_m"] = poseX;
  doc["y_m"] = poseY;
  doc["heading_rad"] = poseHeading;
  doc["left_cm"] = leftDistanceCm;
  doc["right_cm"] = rightDistanceCm;
  doc["battery_v"] = 0.0;  // Add a calibrated voltage-divider input if required.
  doc["error"] = mode == Mode::EMERGENCY ? "physical emergency stop is open" : "";
  doc["mode"] = mode == Mode::PATROL ? "PATROL" :
                mode == Mode::WELCOME ? "WELCOME" :
                mode == Mode::EMERGENCY ? "EMERGENCY" : "STOPPED";
  serializeJson(doc, Serial);
  Serial.println();
}

void handleCommand(const String &line) {
  JsonDocument doc;
  const DeserializationError error = deserializeJson(doc, line);
  if (error) return;
  lastLinkMs = millis();
  const String command = doc["cmd"] | "";

  if (command == "STOP") {
    mode = Mode::STOPPED;
    stopMotors();
  } else if (command == "HEARTBEAT") {
    // Updating lastLinkMs is the entire heartbeat action.
  } else if (command == "SET_MODE") {
    const String requested = doc["mode"] | "";
    if (requested == "PATROL") {
      mode = Mode::PATROL;
      startPatrol();
    } else if (requested == "WELCOME") {
      mode = Mode::WELCOME;
      stopMotors();
    }
  } else if (command == "DRIVE" && mode == Mode::WELCOME) {
    const float linear = doc["linear_mps"] | 0.0f;
    const float angular = doc["angular_rps"] | 0.0f;
    if (linear > 0.0f &&
        min(leftDistanceCm, rightDistanceCm) < OBSTACLE_STOP_CM) {
      stopMotors();
    } else {
      driveRobot(constrain(linear, -0.20f, 0.20f),
                 constrain(angular, -0.70f, 0.70f));
    }
  } else if (command == "WAVE") {
    const float seconds = doc["seconds"] | 2.5f;
    waveUntilMs = millis() + (uint32_t)(constrain(seconds, 0.2f, 5.0f) * 1000);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(LEFT_IN1, OUTPUT);
  pinMode(LEFT_IN2, OUTPUT);
  pinMode(RIGHT_IN1, OUTPUT);
  pinMode(RIGHT_IN2, OUTPUT);
  pinMode(LEFT_PWM, OUTPUT);
  pinMode(RIGHT_PWM, OUTPUT);
  pinMode(LEFT_ENC_A, INPUT);
  pinMode(LEFT_ENC_B, INPUT);
  pinMode(RIGHT_ENC_A, INPUT);
  pinMode(RIGHT_ENC_B, INPUT);
  pinMode(US_LEFT_TRIG, OUTPUT);
  pinMode(US_LEFT_ECHO, INPUT);
  pinMode(US_RIGHT_TRIG, OUTPUT);
  pinMode(US_RIGHT_ECHO, INPUT);
  pinMode(ESTOP_PIN, INPUT_PULLUP);

  attachInterrupt(digitalPinToInterrupt(LEFT_ENC_A), onLeftEncoder, CHANGE);
  attachInterrupt(digitalPinToInterrupt(RIGHT_ENC_A), onRightEncoder, CHANGE);
  leftArm.attach(LEFT_ARM_SERVO, 500, 2500);
  rightArm.attach(RIGHT_ARM_SERVO, 500, 2500);
  leftArm.write(90);
  rightArm.write(90);
  stopMotors();
  lastLinkMs = millis();
}

void loop() {
  while (Serial.available()) {
    const String line = Serial.readStringUntil('\n');
    handleCommand(line);
  }

  updateOdometry();
  leftDistanceCm = readUltrasonicCm(US_LEFT_TRIG, US_LEFT_ECHO);
  rightDistanceCm = readUltrasonicCm(US_RIGHT_TRIG, US_RIGHT_ECHO);
  updateArms();

  if (digitalRead(ESTOP_PIN) == HIGH) {
    mode = Mode::EMERGENCY;
    stopMotors();
  } else if (millis() - lastLinkMs > LINK_TIMEOUT_MS) {
    mode = Mode::STOPPED;
    stopMotors();
  } else if (mode == Mode::PATROL) {
    updatePatrol();
  } else if (mode == Mode::WELCOME &&
             commandedLinear > 0.0f &&
             min(leftDistanceCm, rightDistanceCm) < OBSTACLE_STOP_CM) {
    stopMotors();
  }

  if (millis() - lastTelemetryMs >= TELEMETRY_PERIOD_MS) {
    lastTelemetryMs = millis();
    sendTelemetry();
  }
}
