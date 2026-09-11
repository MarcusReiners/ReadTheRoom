#include <Arduino.h>
#include <math.h>
#include <WiFi.h>
#include <esp_now.h>
#include <ArduinoJson.h>
#include <Adafruit_NeoPixel.h>

// ---------------------------------------------------------------------------
// ESP-NOW <-> USB serial bridge for the mmWave radar project. Also drives a
// 7-LED WS2812B strip (mounted under the desk assistant's "tie") directly,
// since this board's GPIOs are otherwise idle and it already has a serial
// command channel open to the Pi - simpler than a third microcontroller.
//
// Sits between the XIAO ESP32S3 (LD2450 sensor + ESP-NOW sender, see
// ../mmWave/src/main.cpp) and the ReadTheRoom Pi. Plugged into the Pi via
// USB:
//   - Every StatusPacket received over ESP-NOW is re-serialized as one JSON
//     line printed to Serial, for adapters/hardware/radar_ld2450.py to read.
//   - Every JSON command line the Pi writes to Serial is handled locally
//     (set_led) or converted to a CommandPacket and sent to the XIAO over
//     ESP-NOW (set_zone / reset_zone / start_calibration).
//
// Stateless relay - no persistence needed on this board. Devices use
// unicast ESP-NOW with each other's MAC hardcoded (see PEER_MAC below) -
// broadcast was tried first since it needs no pairing, but broadcast
// frames get no 802.11 ACK/auto-retry, which made updates unreliable.
// ---------------------------------------------------------------------------

#define MAX_TARGETS 3

#define LED_PIN 1 // D0
#define LED_COUNT 7

// Unicast to the XIAO's specific MAC (not broadcast) - broadcast frames
// get no 802.11 ACK/auto-retry at the radio level, which made updates
// unreliable; unicast does, and is much more robust over real distance.
static const uint8_t PEER_MAC[6] = {0x90, 0x70, 0x69, 0x11, 0x1B, 0x2C};

Adafruit_NeoPixel strip(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

enum LedMode : uint8_t { LED_MODE_OFF, LED_MODE_SOLID, LED_MODE_PULSE };
static LedMode ledMode = LED_MODE_OFF;
static uint8_t ledR = 0, ledG = 0, ledB = 0;
static unsigned long ledAnimStartMs = 0;
static uint32_t ledPeriodMs = 3000;

// Scales every color the strip is asked to show - applied once here so callers
// (Pi-side handlers.py, test_tie_led.py) can keep using their own full-scale
// color constants without duplicating a brightness cut in each of them.
static const float LED_BRIGHTNESS = 0.5f;

static void applyLedColor(uint8_t r, uint8_t g, uint8_t b) {
  uint8_t sr = (uint8_t)(r * LED_BRIGHTNESS);
  uint8_t sg = (uint8_t)(g * LED_BRIGHTNESS);
  uint8_t sb = (uint8_t)(b * LED_BRIGHTNESS);
  for (int i = 0; i < LED_COUNT; i++) strip.setPixelColor(i, strip.Color(sr, sg, sb));
  strip.show();
}

// PULSE is animated continuously from the main loop; OFF/SOLID are static,
// only need to be (re)drawn once when the mode changes.
static void setLedMode(LedMode mode, uint8_t r, uint8_t g, uint8_t b, uint32_t periodMs) {
  ledMode = mode;
  ledR = r;
  ledG = g;
  ledB = b;
  ledPeriodMs = periodMs;
  ledAnimStartMs = millis();

  if (mode == LED_MODE_OFF) {
    applyLedColor(0, 0, 0);
  } else if (mode == LED_MODE_SOLID) {
    applyLedColor(r, g, b);
  }
}

static void updateLedAnimation() {
  if (ledMode != LED_MODE_PULSE) return;
  // Smooth breathing fade - period is caller-controlled (e.g. slower while
  // idle, faster while listening) so different states can stay visually
  // distinct using animation speed alone, without needing different colors.
  float t = (millis() - ledAnimStartMs) / 1000.0f;
  float periodS = ledPeriodMs / 1000.0f;
  float omega = 2.0f * PI / periodS;
  float brightness = (sinf(t * omega) + 1.0f) / 2.0f;
  applyLedColor((uint8_t)(ledR * brightness), (uint8_t)(ledG * brightness), (uint8_t)(ledB * brightness));
}

struct __attribute__((packed)) TargetWire {
  uint8_t active;
  uint8_t insideZone;
  int16_t x_mm;
  int16_t y_mm;
  int16_t speed_mms;
  uint16_t resolution_mm;
};

// msgType 2 carries the sensor's diagnostics block at the end. The shorter
// msgType 1 packet of the previous sensor firmware is still accepted (its
// diagnostics read as zero), so this bridge can be flashed first.
struct __attribute__((packed)) StatusPacket {
  uint8_t msgType;
  TargetWire targets[MAX_TARGETS];
  uint8_t zoneValid;
  int16_t zoneMinX, zoneMaxX, zoneMinY, zoneMaxY;
  uint8_t calibrating;
  uint32_t calibrationRemainingMs;
  uint8_t zoneMode; // 0 software, 1 module detection, 2 module filter
  uint8_t framesPerSecond;
  uint8_t badFramesPerSecond;
  uint16_t sendFailures;
  uint16_t frameAgeMs;
};

static const size_t LEGACY_STATUS_LEN = offsetof(StatusPacket, framesPerSecond);

enum CommandType : uint8_t {
  CMD_SET_ZONE = 10,
  CMD_RESET_ZONE = 11,
  CMD_START_CALIBRATION = 12,
  CMD_SET_ZONE_MODE = 13,
};

struct __attribute__((packed)) CommandPacket {
  uint8_t msgType;
  int16_t minX, maxX, minY, maxY; // set_zone
  uint32_t durationMs;            // start_calibration
  uint8_t zoneMode;               // set_zone_mode
};

// The ESP-NOW recv callback runs in the WiFi driver's own task, which has
// tight timing constraints - Espressif's docs warn against blocking work
// (like Serial I/O) in there. Doing the JSON serialization + Serial.print
// directly inside this callback caused exactly that: bytes scrambled/dropped
// mid-write, and occasionally a missing '\n' gluing two lines together,
// confirmed on the bench with two different USB cables producing identical
// corruption - it was never the cable. Fix: the callback only copies the
// packet out; loop() does the actual Serial write on the main task.
//
// The hand-over is a one-slot FreeRTOS queue: xQueueOverwrite always keeps
// the newest packet, and unlike the previous shared struct + volatile flag a
// packet can no longer be read by loop() while the WiFi task is halfway
// through overwriting it (targets from two different frames mixed in one).
static QueueHandle_t statusQueue = nullptr;

static void onStatusReceived(const uint8_t *macAddr, const uint8_t *data, int len) {
  if (statusQueue == nullptr) return;
  if (len != (int)sizeof(StatusPacket) && len != (int)LEGACY_STATUS_LEN) return;
  StatusPacket packet = {};
  memcpy(&packet, data, len);
  xQueueOverwrite(statusQueue, &packet);
}

// Lines that did not fit into the USB transmit buffer. When the Pi is not
// reading (app stopped) or falls behind, a blocking write stalled this whole
// loop for ~100 ms per line and let a backlog of old lines build up on the
// Pi. A line that does not fit whole is now dropped instead - never half of
// one, which would glue onto the next line and break its JSON.
static uint32_t droppedLines = 0;
static const size_t USB_TX_BUFFER = 2048;

static void writeStatusPacket(const StatusPacket &packet) {
  JsonDocument doc;

  JsonArray targetsArr = doc["targets"].to<JsonArray>();
  for (int i = 0; i < MAX_TARGETS; i++) {
    if (!packet.targets[i].active) continue;
    JsonObject t = targetsArr.add<JsonObject>();
    t["id"] = i + 1;
    t["x_mm"] = packet.targets[i].x_mm;
    t["y_mm"] = packet.targets[i].y_mm;
    t["speed_mms"] = packet.targets[i].speed_mms;
    t["resolution_mm"] = packet.targets[i].resolution_mm;
    t["in_zone"] = (bool)packet.targets[i].insideZone;
  }

  JsonObject zoneObj = doc["zone"].to<JsonObject>();
  zoneObj["valid"] = (bool)packet.zoneValid;
  if (packet.zoneValid) {
    zoneObj["min_x_mm"] = packet.zoneMinX;
    zoneObj["max_x_mm"] = packet.zoneMaxX;
    zoneObj["min_y_mm"] = packet.zoneMinY;
    zoneObj["max_y_mm"] = packet.zoneMaxY;
  }

  zoneObj["mode"] = packet.zoneMode;

  doc["calibrating"] = (bool)packet.calibrating;
  if (packet.calibrating) {
    doc["remaining_ms"] = packet.calibrationRemainingMs;
  }

  if (packet.msgType >= 2) {
    JsonObject sensor = doc["sensor"].to<JsonObject>();
    sensor["fps"] = packet.framesPerSecond;
    sensor["bad_fps"] = packet.badFramesPerSecond;
    sensor["send_failures"] = packet.sendFailures;
    sensor["frame_age_ms"] = packet.frameAgeMs;
  }
  doc["bridge_dropped"] = droppedLines;

  char line[768];
  size_t n = serializeJson(doc, line, sizeof(line) - 1);
  if (n == 0 || n >= sizeof(line) - 1) return;
  line[n++] = '\n';
  if (Serial.availableForWrite() < (int)n) {
    droppedLines++;
    return;
  }
  Serial.write((const uint8_t *)line, n);
}

static void sendCommand(const CommandPacket &cmd) {
  esp_now_send(PEER_MAC, (const uint8_t *)&cmd, sizeof(cmd));
}

static void handleSerialLine(const String &line) {
  JsonDocument doc;
  if (deserializeJson(doc, line) != DeserializationError::Ok) return;

  const char *cmdName = doc["cmd"] | "";
  CommandPacket cmd = {};

  if (strcmp(cmdName, "set_zone") == 0) {
    cmd.msgType = CMD_SET_ZONE;
    cmd.minX = doc["min_x_mm"] | 0;
    cmd.maxX = doc["max_x_mm"] | 0;
    cmd.minY = doc["min_y_mm"] | 0;
    cmd.maxY = doc["max_y_mm"] | 0;
    sendCommand(cmd);
  } else if (strcmp(cmdName, "reset_zone") == 0) {
    cmd.msgType = CMD_RESET_ZONE;
    sendCommand(cmd);
  } else if (strcmp(cmdName, "start_calibration") == 0) {
    cmd.msgType = CMD_START_CALIBRATION;
    long seconds = doc["seconds"] | 20;
    cmd.durationMs = (uint32_t)(seconds * 1000);
    sendCommand(cmd);
  } else if (strcmp(cmdName, "set_zone_mode") == 0) {
    cmd.msgType = CMD_SET_ZONE_MODE;
    cmd.zoneMode = (uint8_t)(doc["mode"] | 0);
    sendCommand(cmd);
  } else if (strcmp(cmdName, "set_led") == 0) {
    const char *mode = doc["mode"] | "off";
    uint8_t r = doc["r"] | 0;
    uint8_t g = doc["g"] | 0;
    uint8_t b = doc["b"] | 0;
    uint32_t periodMs = doc["period_ms"] | 3000;
    if (strcmp(mode, "solid") == 0) {
      setLedMode(LED_MODE_SOLID, r, g, b, periodMs);
    } else if (strcmp(mode, "pulse") == 0) {
      setLedMode(LED_MODE_PULSE, r, g, b, periodMs);
    } else {
      setLedMode(LED_MODE_OFF, 0, 0, 0, periodMs);
    }
  }
}

static String serialLineBuffer;

static void processSerialInput() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n') {
      serialLineBuffer.trim();
      if (serialLineBuffer.length() > 0) {
        handleSerialLine(serialLineBuffer);
      }
      serialLineBuffer = "";
    } else if (c != '\r') {
      serialLineBuffer += c;
    }
  }
}

void setup() {
  // Must precede begin(): begin() only creates its default 256-byte buffer
  // when none is set yet, and a full status line is longer than that.
  Serial.setTxBufferSize(USB_TX_BUFFER);
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println(F("[Bridge] mmWave ESP-NOW <-> USB serial bridge"));

  strip.begin();
  strip.show();
  Serial.println(F("[LED] Strip ready"));

  WiFi.mode(WIFI_STA);
  if (esp_now_init() != ESP_OK) {
    Serial.println(F("[ESP-NOW] Init failed"));
    return;
  }

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, PEER_MAC, 6);
  peer.channel = 0;
  peer.encrypt = false;
  if (esp_now_add_peer(&peer) != ESP_OK) {
    Serial.println(F("[ESP-NOW] Failed to add XIAO peer"));
  }

  statusQueue = xQueueCreate(1, sizeof(StatusPacket));
  esp_now_register_recv_cb(onStatusReceived);
  Serial.println(F("[ESP-NOW] Ready, waiting for XIAO status packets"));
  Serial.print(F("[ESP-NOW] This device's MAC: "));
  Serial.println(WiFi.macAddress());
}

void loop() {
  processSerialInput();

  StatusPacket packet;
  if (statusQueue != nullptr && xQueueReceive(statusQueue, &packet, 0) == pdTRUE) {
    writeStatusPacket(packet);
  }

  static unsigned long lastLedUpdateMs = 0;
  unsigned long now = millis();
  if (now - lastLedUpdateMs >= 30) {
    updateLedAnimation();
    lastLedUpdateMs = now;
  }
}
