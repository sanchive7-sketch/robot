/*
 * Manual-only Wi-Fi ESP32 controller — no ultrasonic sensors, no encoders.
 *
 * This is a separate sketch. It does not modify the original USB firmware or
 * the sensor-enabled Wi-Fi version. It accepts only manual movement commands;
 * autonomous Welcome commands are deliberately ignored.
 *
 * REQUIRED: a normally-closed physical E-stop from GPIO 21 to GND, plus a
 * separate contact that removes motor-driver battery power.
 */
#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include <WiFi.h>
#include <WiFiUdp.h>

#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#else
#error "Create wifi_secrets.h by copying wifi_secrets.h.example, then set Wi-Fi values."
#endif

// Motor driver pins: left motor pair and right motor pair.
constexpr uint8_t LEFT_PWM = 25;
constexpr uint8_t LEFT_IN1 = 26;
constexpr uint8_t LEFT_IN2 = 27;
constexpr uint8_t RIGHT_PWM = 33;
constexpr uint8_t RIGHT_IN1 = 32;
constexpr uint8_t RIGHT_IN2 = 4;
constexpr uint8_t SHOULDER_SERVO_PIN = 13;
constexpr uint8_t FOREARM_SERVO_PIN = 14;
constexpr uint8_t ESTOP_PIN = 21;

constexpr bool HAS_ARM_SERVOS = true;
constexpr bool LEFT_MOTOR_REVERSED = false;
constexpr bool RIGHT_MOTOR_REVERSED = true;
constexpr uint16_t ESP32_COMMAND_PORT = 9010;
constexpr uint16_t LAPTOP_TELEMETRY_PORT = 9011;
constexpr uint32_t LINK_TIMEOUT_MS = 1200;
constexpr uint32_t MANUAL_COMMAND_TIMEOUT_MS = 500;
constexpr uint32_t TELEMETRY_PERIOD_MS = 200;

enum class Mode { STOPPED, MANUAL, EMERGENCY };

WiFiUDP udp;
Servo shoulderServo;
Servo forearmServo;
Mode mode = Mode::STOPPED;
String queuedCommand;
uint32_t lastConnectAttemptMs = 0;
uint32_t lastLinkMs = 0;
uint32_t lastManualDriveMs = 0;
uint32_t lastTelemetryMs = 0;
uint32_t waveUntilMs = 0;
uint32_t lastWavePhaseMs = 0;
bool wavePhase = false;
float commandedLinear = 0.0f;
bool udpStarted = false;

void connectWifi() {
  lastConnectAttemptMs = millis();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void maintainWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    if (!udpStarted) {
      udp.begin(ESP32_COMMAND_PORT);
      udpStarted = true;
    }
    return;
  }
  udpStarted = false;
  if (millis() - lastConnectAttemptMs >= 5000) connectWifi();
}

void setMotor(uint8_t pwmPin, uint8_t in1, uint8_t in2, int pwm, bool reversed) {
  pwm = constrain(pwm, -255, 255);
  if (reversed) pwm = -pwm;
  if (pwm == 0) {
    digitalWrite(in1, LOW);
    digitalWrite(in2, LOW);
    analogWrite(pwmPin, 0);
    return;
  }
  digitalWrite(in1, pwm > 0 ? HIGH : LOW);
  digitalWrite(in2, pwm > 0 ? LOW : HIGH);
  analogWrite(pwmPin, abs(pwm));
}

void driveRobot(float linearMps, float angularRps) {
  commandedLinear = linearMps;
  constexpr float TRACK_WIDTH_M = 0.315f;
  constexpr float MAX_WHEEL_MPS = 0.45f;
  const float leftMps = linearMps - angularRps * TRACK_WIDTH_M * 0.5f;
  const float rightMps = linearMps + angularRps * TRACK_WIDTH_M * 0.5f;
  setMotor(LEFT_PWM, LEFT_IN1, LEFT_IN2, (int)(255.0f * leftMps / MAX_WHEEL_MPS),
           LEFT_MOTOR_REVERSED);
  setMotor(RIGHT_PWM, RIGHT_IN1, RIGHT_IN2, (int)(255.0f * rightMps / MAX_WHEEL_MPS),
           RIGHT_MOTOR_REVERSED);
}

void stopMotors() { driveRobot(0.0f, 0.0f); }

void receiveCommand() {
  const int packetSize = udp.parsePacket();
  if (packetSize <= 0 || packetSize >= 1024) {
    while (udp.available()) udp.read();
    return;
  }
  String incoming;
  incoming.reserve(packetSize);
  while (udp.available()) incoming += static_cast<char>(udp.read());
  JsonDocument doc;
  if (deserializeJson(doc, incoming) ||
      strcmp(doc["token"] | "", WIFI_SHARED_TOKEN) != 0) {
    return;
  }
  queuedCommand = incoming;
}

void handleCommand() {
  JsonDocument doc;
  if (deserializeJson(doc, queuedCommand)) return;
  queuedCommand = "";
  lastLinkMs = millis();
  const String command = doc["cmd"] | "";

  if (command == "STOP") {
    mode = Mode::STOPPED;
    stopMotors();
  } else if (command == "SET_MODE" && String(doc["mode"] | "") == "MANUAL") {
    mode = Mode::MANUAL;
    lastManualDriveMs = millis();
    stopMotors();
  } else if (command == "MANUAL_DRIVE" && mode == Mode::MANUAL) {
    const float linear = constrain((float)(doc["linear_mps"] | 0.0f), -0.07f, 0.10f);
    const float angular = constrain((float)(doc["angular_rps"] | 0.0f), -0.45f, 0.45f);
    lastManualDriveMs = millis();
    driveRobot(linear, angular);
  } else if (command == "WAVE" && HAS_ARM_SERVOS) {
    waveUntilMs = millis() + 2500;
  }
}

void updateArms() {
  if (!HAS_ARM_SERVOS) return;
  if (millis() >= waveUntilMs) {
    shoulderServo.write(0);
    forearmServo.write(90);
    return;
  }
  shoulderServo.write(90);
  if (millis() - lastWavePhaseMs >= 260) {
    lastWavePhaseMs = millis();
    wavePhase = !wavePhase;
    forearmServo.write(wavePhase ? 45 : 135);
  }
}

void sendTelemetry() {
  if (WiFi.status() != WL_CONNECTED || !udpStarted) return;
  JsonDocument doc;
  doc["type"] = "telemetry";
  doc["token"] = WIFI_SHARED_TOKEN;
  doc["x_m"] = 0.0;
  doc["y_m"] = 0.0;
  doc["heading_rad"] = 0.0;
  doc["left_cm"] = 999.0;
  doc["right_cm"] = 999.0;
  doc["battery_v"] = 0.0;
  doc["error"] = mode == Mode::EMERGENCY ? "physical emergency stop is open" : "";
  doc["mode"] = mode == Mode::MANUAL ? "MANUAL" :
                mode == Mode::EMERGENCY ? "EMERGENCY" : "STOPPED";
  udp.beginPacket(IPAddress(255, 255, 255, 255), LAPTOP_TELEMETRY_PORT);
  serializeJson(doc, udp);
  udp.endPacket();
}

void setup() {
  pinMode(LEFT_IN1, OUTPUT);
  pinMode(LEFT_IN2, OUTPUT);
  pinMode(RIGHT_IN1, OUTPUT);
  pinMode(RIGHT_IN2, OUTPUT);
  pinMode(LEFT_PWM, OUTPUT);
  pinMode(RIGHT_PWM, OUTPUT);
  pinMode(ESTOP_PIN, INPUT_PULLUP);
  if (HAS_ARM_SERVOS) {
    shoulderServo.attach(SHOULDER_SERVO_PIN, 500, 2500);
    forearmServo.attach(FOREARM_SERVO_PIN, 500, 2500);
  }
  stopMotors();
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  connectWifi();
  lastLinkMs = millis();
}

void loop() {
  maintainWifi();
  receiveCommand();
  if (queuedCommand.length()) handleCommand();
  updateArms();

  if (digitalRead(ESTOP_PIN) == HIGH) {
    mode = Mode::EMERGENCY;
    stopMotors();
  } else if (millis() - lastLinkMs > LINK_TIMEOUT_MS) {
    mode = Mode::STOPPED;
    stopMotors();
  } else if (mode == Mode::MANUAL &&
             millis() - lastManualDriveMs > MANUAL_COMMAND_TIMEOUT_MS) {
    stopMotors();
  }
  if (millis() - lastTelemetryMs >= TELEMETRY_PERIOD_MS) {
    lastTelemetryMs = millis();
    sendTelemetry();
  }
}
