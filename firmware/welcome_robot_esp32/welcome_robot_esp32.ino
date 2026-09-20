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
constexpr uint8_t SHOULDER_SERVO_PIN = 13;  // Servo 1.
constexpr uint8_t FOREARM_SERVO_PIN = 14;   // Servo 2.
constexpr uint8_t ESTOP_PIN = 21;  // Normally-closed switch to GND.

// ----- Installed hardware profile -----
// Current build: motors, ultrasonic sensors and one articulated arm. Change a
// flag to true only after that component is wired and tested. Welcome motion
// requires both encoders and ultrasonic sensors to enforce the fixed safe area.
constexpr bool HAS_ENCODERS = false;
constexpr bool HAS_ULTRASONIC_SENSORS = true;
constexpr bool HAS_ARM_SERVOS = true;
constexpr bool HAS_PHYSICAL_ESTOP = false;

// ----- Welcome arm gesture -----
constexpr int SHOULDER_REST_ANGLE = 0;
constexpr int SHOULDER_RAISED_ANGLE = 90;
constexpr int FOREARM_CENTER_ANGLE = 90;
constexpr int FOREARM_LEFT_ANGLE = 45;
constexpr int FOREARM_RIGHT_ANGLE = 135;
constexpr uint8_t FOREARM_WAVE_COUNT = 3;
// One degree every 20 ms gives a slow, smooth movement without blocking the
// serial heartbeat, motor safety checks or ultrasonic polling.
constexpr uint32_t SERVO_STEP_INTERVAL_MS = 20;

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
constexpr float WELCOME_ZONE_MARGIN_M = 0.06f;
constexpr float WELCOME_COMMAND_HORIZON_S = 0.50f;
constexpr uint32_t MANUAL_COMMAND_TIMEOUT_MS = 500;
constexpr uint32_t LINK_TIMEOUT_MS = 1200;
constexpr uint32_t TELEMETRY_PERIOD_MS = 200;

enum class Mode { STOPPED, WELCOME, MANUAL, EMERGENCY };
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
enum class WelcomeGestureState {
  IDLE,
  RAISING_SHOULDER,
  WAVING_LEFT,
  WAVING_RIGHT,
  CENTERING_FOREARM,
  LOWERING_SHOULDER
};

volatile int32_t leftTicks = 0;
volatile int32_t rightTicks = 0;
int32_t previousLeftTicks = 0;
int32_t previousRightTicks = 0;

Mode mode = Mode::STOPPED;
PatrolState patrolState = PatrolState::DRIVE_LEG;
WelcomeGestureState welcomeGestureState = WelcomeGestureState::IDLE;
Servo shoulderServo;
Servo forearmServo;

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
float welcomeOriginX = 0.0f;
float welcomeOriginY = 0.0f;
float welcomeOriginHeading = 0.0f;
float welcomeForwardLimitM = 1.20f;
float welcomeSideLimitM = 0.60f;
float commandedLinear = 0.0f;
float commandedAngular = 0.0f;
uint32_t lastLinkMs = 0;
uint32_t lastManualDriveMs = 0;
uint32_t lastTelemetryMs = 0;
uint32_t lastServoStepMs = 0;
int shoulderAngle = SHOULDER_REST_ANGLE;
int forearmAngle = FOREARM_CENTER_ANGLE;
uint8_t completedForearmWaves = 0;

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

void startWelcome() {
  // Every Welcome session gets a fresh local coordinate system, so a reset or
  // odometry drift cannot silently turn an old global pose into a new boundary.
  welcomeOriginX = poseX;
  welcomeOriginY = poseY;
  welcomeOriginHeading = poseHeading;
  stopMotors();
}

bool isInsideWelcomeZone(float x, float y, float margin) {
  const float dx = x - welcomeOriginX;
  const float dy = y - welcomeOriginY;
  const float forward = dx * cosf(welcomeOriginHeading) +
                        dy * sinf(welcomeOriginHeading);
  const float side = -dx * sinf(welcomeOriginHeading) +
                     dy * cosf(welcomeOriginHeading);
  return fabsf(forward) <= welcomeForwardLimitM - margin &&
         fabsf(side) <= welcomeSideLimitM - margin;
}

bool welcomeDriveAllowed(float linearMps) {
  if (linearMps == 0.0f) return true;  // Turning in place stays in the zone.
  const float horizon = linearMps * WELCOME_COMMAND_HORIZON_S;
  const float projectedX = poseX + horizon * cosf(poseHeading);
  const float projectedY = poseY + horizon * sinf(poseHeading);
  return isInsideWelcomeZone(projectedX, projectedY, WELCOME_ZONE_MARGIN_M);
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

bool moveServoOneDegree(Servo &servo, int &currentAngle, int targetAngle) {
  if (currentAngle == targetAngle) return true;
  currentAngle += currentAngle < targetAngle ? 1 : -1;
  servo.write(currentAngle);
  return currentAngle == targetAngle;
}

void startWelcomeGesture() {
  // Do not restart a gesture midway because that would make either joint jerk.
  if (welcomeGestureState != WelcomeGestureState::IDLE) return;
  forearmAngle = FOREARM_CENTER_ANGLE;
  forearmServo.write(forearmAngle);
  completedForearmWaves = 0;
  lastServoStepMs = millis();
  welcomeGestureState = WelcomeGestureState::RAISING_SHOULDER;
}

void updateArms() {
  if (welcomeGestureState == WelcomeGestureState::IDLE ||
      millis() - lastServoStepMs < SERVO_STEP_INTERVAL_MS) {
    return;
  }
  lastServoStepMs = millis();

  switch (welcomeGestureState) {
    case WelcomeGestureState::RAISING_SHOULDER:
      if (moveServoOneDegree(
              shoulderServo, shoulderAngle, SHOULDER_RAISED_ANGLE)) {
        welcomeGestureState = WelcomeGestureState::WAVING_LEFT;
      }
      break;
    case WelcomeGestureState::WAVING_LEFT:
      if (moveServoOneDegree(forearmServo, forearmAngle, FOREARM_LEFT_ANGLE)) {
        welcomeGestureState = WelcomeGestureState::WAVING_RIGHT;
      }
      break;
    case WelcomeGestureState::WAVING_RIGHT:
      if (moveServoOneDegree(forearmServo, forearmAngle, FOREARM_RIGHT_ANGLE)) {
        completedForearmWaves++;
        welcomeGestureState = completedForearmWaves >= FOREARM_WAVE_COUNT
                                  ? WelcomeGestureState::CENTERING_FOREARM
                                  : WelcomeGestureState::WAVING_LEFT;
      }
      break;
    case WelcomeGestureState::CENTERING_FOREARM:
      if (moveServoOneDegree(
              forearmServo, forearmAngle, FOREARM_CENTER_ANGLE)) {
        welcomeGestureState = WelcomeGestureState::LOWERING_SHOULDER;
      }
      break;
    case WelcomeGestureState::LOWERING_SHOULDER:
      if (moveServoOneDegree(
              shoulderServo, shoulderAngle, SHOULDER_REST_ANGLE)) {
        welcomeGestureState = WelcomeGestureState::IDLE;
      }
      break;
    case WelcomeGestureState::IDLE:
      break;
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
  doc["mode"] = mode == Mode::MANUAL ? "MANUAL" :
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
  } else if (command == "SET_WELCOME_ZONE") {
    // The laptop and ESP32 both enforce this. The firmware remains the final
    // guard if the laptop process stalls or sends a bad command.
    welcomeForwardLimitM = constrain(
        doc["forward_limit_m"] | 1.20f, WELCOME_ZONE_MARGIN_M + 0.05f, 3.0f);
    welcomeSideLimitM = constrain(
        doc["side_limit_m"] | 0.60f, WELCOME_ZONE_MARGIN_M + 0.05f, 3.0f);
  } else if (command == "SET_MODE") {
    const String requested = doc["mode"] | "";
    if (requested == "MANUAL") {
      mode = Mode::MANUAL;
      lastManualDriveMs = millis();
      stopMotors();
    } else if (requested == "WELCOME" && HAS_ENCODERS &&
               HAS_ULTRASONIC_SENSORS) {
      mode = Mode::WELCOME;
      startWelcome();
    } else if (requested == "WELCOME") {
      // Greeting and speech still run on the laptop, but motor-only hardware
      // must not attempt autonomous motion without boundary/obstacle feedback.
      mode = Mode::STOPPED;
      stopMotors();
    }
  } else if (command == "MANUAL_DRIVE" && mode == Mode::MANUAL) {
    const float linear = constrain(
        (float)(doc["linear_mps"] | 0.0f), -0.07f, 0.10f);
    const float angular = constrain(
        (float)(doc["angular_rps"] | 0.0f), -0.45f, 0.45f);
    lastManualDriveMs = millis();
    if (linear > 0.0f &&
        min(leftDistanceCm, rightDistanceCm) < OBSTACLE_STOP_CM) {
      stopMotors();
    } else {
      driveRobot(linear, angular);
    }
  } else if (command == "DRIVE" && mode == Mode::WELCOME) {
    const float linear = doc["linear_mps"] | 0.0f;
    const float angular = doc["angular_rps"] | 0.0f;
    if ((linear > 0.0f &&
         min(leftDistanceCm, rightDistanceCm) < OBSTACLE_STOP_CM) ||
        !welcomeDriveAllowed(linear)) {
      stopMotors();
    } else {
      driveRobot(constrain(linear, -0.20f, 0.20f),
                 constrain(angular, -0.70f, 0.70f));
    }
  } else if (command == "WAVE" && HAS_ARM_SERVOS) {
    startWelcomeGesture();
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
  if (HAS_ENCODERS) {
    pinMode(LEFT_ENC_A, INPUT);
    pinMode(LEFT_ENC_B, INPUT);
    pinMode(RIGHT_ENC_A, INPUT);
    pinMode(RIGHT_ENC_B, INPUT);
    attachInterrupt(digitalPinToInterrupt(LEFT_ENC_A), onLeftEncoder, CHANGE);
    attachInterrupt(digitalPinToInterrupt(RIGHT_ENC_A), onRightEncoder, CHANGE);
  }
  if (HAS_ULTRASONIC_SENSORS) {
    pinMode(US_LEFT_TRIG, OUTPUT);
    pinMode(US_LEFT_ECHO, INPUT);
    pinMode(US_RIGHT_TRIG, OUTPUT);
    pinMode(US_RIGHT_ECHO, INPUT);
  }
  if (HAS_PHYSICAL_ESTOP) {
    pinMode(ESTOP_PIN, INPUT_PULLUP);
  }
  if (HAS_ARM_SERVOS) {
    shoulderServo.attach(SHOULDER_SERVO_PIN, 500, 2500);
    forearmServo.attach(FOREARM_SERVO_PIN, 500, 2500);
    shoulderServo.write(SHOULDER_REST_ANGLE);
    forearmServo.write(FOREARM_CENTER_ANGLE);
  }
  stopMotors();
  lastLinkMs = millis();
}

void loop() {
  while (Serial.available()) {
    const String line = Serial.readStringUntil('\n');
    handleCommand(line);
  }

  if (HAS_ENCODERS) {
    updateOdometry();
  }
  if (HAS_ULTRASONIC_SENSORS) {
    leftDistanceCm = readUltrasonicCm(US_LEFT_TRIG, US_LEFT_ECHO);
    rightDistanceCm = readUltrasonicCm(US_RIGHT_TRIG, US_RIGHT_ECHO);
  }
  if (HAS_ARM_SERVOS) {
    updateArms();
  }

  if (HAS_PHYSICAL_ESTOP && digitalRead(ESTOP_PIN) == HIGH) {
    mode = Mode::EMERGENCY;
    stopMotors();
  } else if (millis() - lastLinkMs > LINK_TIMEOUT_MS) {
    mode = Mode::STOPPED;
    stopMotors();
  } else if (mode == Mode::MANUAL &&
             (millis() - lastManualDriveMs > MANUAL_COMMAND_TIMEOUT_MS ||
              (commandedLinear > 0.0f &&
               min(leftDistanceCm, rightDistanceCm) < OBSTACLE_STOP_CM))) {
    // Manual motion has a separate lease. Laptop heartbeats cannot keep an
    // abandoned movement command alive after the operator releases control.
    stopMotors();
  } else if (mode == Mode::WELCOME &&
             ((commandedLinear > 0.0f &&
               min(leftDistanceCm, rightDistanceCm) < OBSTACLE_STOP_CM) ||
              !isInsideWelcomeZone(poseX, poseY, 0.0f))) {
    // This executes continuously, not only when a new DRIVE message arrives.
    // Encoder overshoot therefore stops at the fixed welcome boundary.
    stopMotors();
  }

  if (millis() - lastTelemetryMs >= TELEMETRY_PERIOD_MS) {
    lastTelemetryMs = millis();
    sendTelemetry();
  }
}
