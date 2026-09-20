/*
 * Wi-Fi version of the College Welcome Robot ESP32 controller.
 *
 * This sketch deliberately reuses the original motion/safety code without
 * changing it.  Only the serial transport is replaced by authenticated UDP.
 * Upload this sketch for Wi-Fi control; upload the original sketch for USB.
 *
 * Before compiling:
 *   1. Copy wifi_secrets.h.example to wifi_secrets.h in this folder.
 *   2. Enter the Wi-Fi network and a long random shared token.
 *   3. Put the same token in ROBOT_WIFI_TOKEN in the laptop's .env.
 */
#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include <WiFi.h>
#include <WiFiUdp.h>

#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#else
#error "Create wifi_secrets.h by copying wifi_secrets.h.example, then fill in Wi-Fi settings."
#endif

constexpr uint16_t ESP32_COMMAND_PORT = 9010;
constexpr uint16_t LAPTOP_TELEMETRY_PORT = 9011;
constexpr size_t MAX_WIFI_PACKET_BYTES = 1024;

class RobotWifiTransport {
 public:
  void begin(unsigned long) {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    connect();
  }

  int available() {
    maintainConnection();
    if (queuedCommand_.length()) return queuedCommand_.length();
    if (WiFi.status() != WL_CONNECTED) return 0;

    const int packetSize = udp_.parsePacket();
    if (packetSize <= 0 || packetSize >= MAX_WIFI_PACKET_BYTES) {
      while (udp_.available()) udp_.read();
      return 0;
    }
    String incoming;
    incoming.reserve(packetSize);
    while (udp_.available()) incoming += static_cast<char>(udp_.read());

    JsonDocument doc;
    if (deserializeJson(doc, incoming) ||
        strcmp(doc["token"] | "", WIFI_SHARED_TOKEN) != 0) {
      return 0;
    }
    // The reused controller parses this JSON and deliberately ignores "token".
    queuedCommand_ = incoming;
    return queuedCommand_.length();
  }

  String readStringUntil(char) {
    String command = queuedCommand_;
    queuedCommand_ = "";
    return command;
  }

  size_t write(uint8_t byte) {
    if (outgoing_.length() >= MAX_WIFI_PACKET_BYTES - 1) return 0;
    outgoing_ += static_cast<char>(byte);
    return 1;
  }

  size_t write(const uint8_t *buffer, size_t size) {
    size_t written = 0;
    while (written < size && write(buffer[written])) ++written;
    return written;
  }

  size_t println() {
    sendTelemetry();
    return 1;
  }

 private:
  WiFiUDP udp_;
  bool udpStarted_ = false;
  uint32_t lastConnectAttemptMs_ = 0;
  String queuedCommand_;
  String outgoing_;

  void connect() {
    lastConnectAttemptMs_ = millis();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }

  void maintainConnection() {
    if (WiFi.status() == WL_CONNECTED) {
      if (!udpStarted_) {
        udp_.begin(ESP32_COMMAND_PORT);
        udpStarted_ = true;
      }
      return;
    }
    udpStarted_ = false;
    if (millis() - lastConnectAttemptMs_ >= 5000) connect();
  }

  void sendTelemetry() {
    maintainConnection();
    if (WiFi.status() == WL_CONNECTED && udpStarted_ && outgoing_.length()) {
      JsonDocument doc;
      if (!deserializeJson(doc, outgoing_)) {
        doc["token"] = WIFI_SHARED_TOKEN;
        // Broadcast lets the laptop learn the ESP32 IP automatically. It works
        // on ordinary private Wi-Fi; client-isolated public/hotspot networks
        // are intentionally unsuitable for robot control.
        udp_.beginPacket(IPAddress(255, 255, 255, 255), LAPTOP_TELEMETRY_PORT);
        serializeJson(doc, udp_);
        udp_.endPacket();
      }
    }
    outgoing_ = "";
  }
};

RobotWifiTransport RobotWifiLink;

// All original Serial calls below now use the Wi-Fi transport.  Arduino and
// library headers were included above, so their declarations are not renamed.
// The Arduino IDE normally generates this forward declaration for the original
// top-level sketch.  It is needed explicitly because that sketch is included
// by this separate Wi-Fi version.
void stopMotors();
#define Serial RobotWifiLink
#include "../welcome_robot_esp32/welcome_robot_esp32.ino"
#undef Serial
