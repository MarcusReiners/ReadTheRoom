#include <Arduino.h>
#include <math.h>
#include <WiFi.h>
#include <esp_now.h>
#include <Preferences.h>

// ---------------------------------------------------------------------------
// HLK-LD2450 mmWave radar <-> Seeed XIAO ESP32S3 UART2
//
// Wiring:
//   LD2450 TX  -> XIAO D7 / GPIO44 (UART2 RX)
//   LD2450 RX  -> XIAO D6 / GPIO43 (UART2 TX)
//   LD2450 5V  -> XIAO 5V
//   LD2450 GND -> XIAO GND
//
// Coordinate system (as reported by the sensor, standard/report mode):
//   X negative = left of sensor,  X positive = right of sensor
//   Y positive = distance in front of the sensor
//   Units are millimeters.
//
// Readings are sent over ESP-NOW (unicast to the bridge's MAC, see
// PEER_MAC below) to a bridge ESP32 that relays them to the ReadTheRoom
// Pi over USB serial - see mmWaveBridge/. ESP-NOW was chosen over plain
// WiFi/HTTP because it skips AP association/DHCP entirely, avoiding the
// flaky-router/hotspot-isolation issues that plagued the WiFi version.
// Unicast (not broadcast) because broadcast frames get no 802.11
// ACK/auto-retry, which made updates unreliable over real distance.
// ---------------------------------------------------------------------------

#define LD2450_BAUD 256000
#define LD2450_RX_PIN 44
#define LD2450_TX_PIN 43
#define LD2450_MAX_TARGETS 3

HardwareSerial ld2450Serial(2);
Preferences prefs;

// Unicast to the bridge's specific MAC (not broadcast) - broadcast frames
// get no 802.11 ACK/auto-retry at the radio level, which made updates
// unreliable; unicast does, and is much more robust over real distance.
static const uint8_t PEER_MAC[6] = {0x90, 0x70, 0x69, 0x12, 0x77, 0x5C};

struct RadarTarget {
  bool active;
  bool insideZone;
  int16_t x_mm;
  int16_t y_mm;
  int16_t speed_mms;
  uint16_t resolution_mm;
};

// ---------------------------------------------------------------------------
// Room zone: an axis-aligned rectangle (in sensor mm coordinates) that
// targets must fall inside to be reported. Set via a calibration walk
// (see startCalibration below) and persisted to flash so it survives
// reboots.
// ---------------------------------------------------------------------------
struct Zone {
  bool valid;
  int16_t minX, maxX, minY, maxY;
};

static Zone zone = {false, 0, 0, 0, 0};

// Where the zone rectangle is enforced.
//   SOFTWARE - the module reports everything it sees and this sketch tags
//              each target with insideZone. Full visibility: the settings
//              page can show out-of-zone targets, which is how you check the
//              sensor is really seeing three people at all.
//   DETECTION - pushed to the module's own zone registers; it then only
//              reports targets inside the rectangle. Out-of-zone targets
//              become invisible to us entirely.
//   FILTER   - same registers, inverted: the module reports everything
//              EXCEPT what's inside the rectangle. This is the one to use
//              against a persistent noise source (a doorway, a fan, a
//              reflective window) - draw the box around the troublemaker.
//
// The module applies one type to all three of its zone slots at once, so
// this is necessarily a single global mode rather than per-zone.
enum ZoneMode : uint8_t {
  ZONE_MODE_SOFTWARE = 0,
  ZONE_MODE_DETECTION = 1,
  ZONE_MODE_FILTER = 2,
};

static uint8_t zoneMode = ZONE_MODE_SOFTWARE;

// Defined further down, once the command-frame helpers it needs exist -
// declared here so the zone-changing functions above it can re-push.
static void sendZoneToModule();

// Extra padding added around the walked corners so the zone isn't a knife
// edge right at the calibration points.
static const int16_t ZONE_MARGIN_MM = 300;
static const unsigned long CALIBRATION_DEFAULT_MS = 20000;

static bool calibrating = false;
static unsigned long calibrationEndMs = 0;
static bool calibrationHasData = false;
static int16_t calMinX, calMaxX, calMinY, calMaxY;

static void loadZone() {
  prefs.begin("zone", true);
  zone.valid = prefs.getBool("valid", false);
  zone.minX = prefs.getShort("minX", 0);
  zone.maxX = prefs.getShort("maxX", 0);
  zone.minY = prefs.getShort("minY", 0);
  zone.maxY = prefs.getShort("maxY", 0);
  zoneMode = prefs.getUChar("mode", ZONE_MODE_SOFTWARE);
  prefs.end();
}

static void saveZone() {
  prefs.begin("zone", false);
  prefs.putBool("valid", zone.valid);
  prefs.putShort("minX", zone.minX);
  prefs.putShort("maxX", zone.maxX);
  prefs.putShort("minY", zone.minY);
  prefs.putShort("maxY", zone.maxY);
  prefs.putUChar("mode", zoneMode);
  prefs.end();
}

static bool isInsideZone(int16_t x, int16_t y) {
  return x >= zone.minX && x <= zone.maxX && y >= zone.minY && y <= zone.maxY;
}

static void startCalibration(unsigned long durationMs) {
  calibrating = true;
  calibrationHasData = false;
  calibrationEndMs = millis() + durationMs;
  calMinX = calMinY = INT16_MAX;
  calMaxX = calMaxY = INT16_MIN;

  Serial.println();
  Serial.print(F("[Calibration] Started - walk to all corners of the room for "));
  Serial.print(durationMs / 1000);
  Serial.println(F(" seconds"));
}

static void finishCalibration() {
  calibrating = false;

  if (!calibrationHasData) {
    Serial.println(F("[Calibration] No targets seen during calibration, zone unchanged"));
    return;
  }

  zone.valid = true;
  zone.minX = calMinX - ZONE_MARGIN_MM;
  zone.maxX = calMaxX + ZONE_MARGIN_MM;
  zone.minY = calMinY - ZONE_MARGIN_MM;
  zone.maxY = calMaxY + ZONE_MARGIN_MM;
  saveZone();
  sendZoneToModule();

  Serial.println(F("[Calibration] Done, zone saved:"));
  Serial.print(F("  X: "));
  Serial.print(zone.minX);
  Serial.print(F(" .. "));
  Serial.println(zone.maxX);
  Serial.print(F("  Y: "));
  Serial.print(zone.minY);
  Serial.print(F(" .. "));
  Serial.println(zone.maxY);
}

static void resetZone() {
  zone.valid = false;
  saveZone();
  // Clears the module's own registers too, otherwise a previously pushed
  // Detection zone would keep hiding targets with no zone left on the Pi
  // side to explain why.
  sendZoneToModule();
  Serial.println(F("[Calibration] Zone reset, tracking is unfiltered"));
}

static void setZoneDirect(int16_t minX, int16_t maxX, int16_t minY, int16_t maxY) {
  if (minX >= maxX || minY >= maxY) {
    Serial.println(F("[Zone] Rejected set_zone command: min must be less than max"));
    return;
  }
  zone.valid = true;
  zone.minX = minX;
  zone.maxX = maxX;
  zone.minY = minY;
  zone.maxY = maxY;
  saveZone();
  sendZoneToModule();
  Serial.println(F("[Zone] Set via ESP-NOW command"));
}

// ---------------------------------------------------------------------------
// LD2450 configuration protocol (command frames, distinct from the data
// frames below). Byte values cross-checked against ESPHome's ld2450
// component, which is the most heavily field-tested implementation of this
// protocol.
//
// Frame layout:  FD FC FB FA | len_lo len_hi | cmd_lo cmd_hi | value... | 04 03 02 01
// where len counts the 2 command bytes plus any value bytes.
//
// Commands must be wrapped in enable-config / end-config, and the module
// needs a moment to apply each one before the next arrives.
// ---------------------------------------------------------------------------
static const uint8_t CMD_FRAME_HEADER[4] = {0xFD, 0xFC, 0xFB, 0xFA};
static const uint8_t CMD_FRAME_FOOTER[4] = {0x04, 0x03, 0x02, 0x01};

static const uint8_t CMD_ENABLE_CONF = 0xFF;
static const uint8_t CMD_DISABLE_CONF = 0xFE;
static const uint8_t CMD_QUERY_VERSION = 0xA0;
static const uint8_t CMD_MULTI_TARGET_MODE = 0x90;
static const uint8_t CMD_QUERY_TARGET_MODE = 0x91;

static void sendConfigCommand(uint8_t command, const uint8_t *value, uint8_t valueLen) {
  ld2450Serial.write(CMD_FRAME_HEADER, sizeof(CMD_FRAME_HEADER));
  uint8_t len = 2 + valueLen;
  uint8_t lenCmd[4] = {len, 0x00, command, 0x00};
  ld2450Serial.write(lenCmd, sizeof(lenCmd));
  if (value != nullptr && valueLen > 0) {
    ld2450Serial.write(value, valueLen);
  }
  ld2450Serial.write(CMD_FRAME_FOOTER, sizeof(CMD_FRAME_FOOTER));
  ld2450Serial.flush();
  delay(100);
}

// Reads whatever the module replies with and dumps the start of it as hex.
// The replies are only needed at boot for diagnostics (firmware version,
// current target mode), so this deliberately doesn't build a parser - it
// drains the buffer so the frame state machine in loop() starts clean.
// DRAIN_MAX_MS is a hard stop: once the module is back in normal operation
// it streams data frames with gaps shorter than the 100 ms quiet window, and
// without the cap this loop never returned - setup() hung before ESP-NOW
// started and the sensor never sent a single packet.
static const unsigned long DRAIN_MAX_MS = 600;
static const size_t DRAIN_PRINT_BYTES = 48;

static size_t drainConfigReply(const char *label, uint8_t *capture = nullptr, size_t captureSize = 0) {
  unsigned long start = millis();
  unsigned long deadline = start + 300;
  size_t count = 0;
  while (millis() < deadline && millis() - start < DRAIN_MAX_MS) {
    while (ld2450Serial.available() > 0 && millis() - start < DRAIN_MAX_MS) {
      if (count == 0) {
        Serial.print(F("[LD2450] "));
        Serial.print(label);
        Serial.print(F(" reply:"));
      }
      uint8_t b = (uint8_t)ld2450Serial.read();
      if (capture != nullptr && count < captureSize) {
        capture[count] = b;
      }
      if (count < DRAIN_PRINT_BYTES) {
        Serial.print(b < 0x10 ? F(" 0") : F(" "));
        Serial.print(b, HEX);
      }
      count++;
      deadline = millis() + 100;
    }
  }
  if (count > DRAIN_PRINT_BYTES) {
    Serial.print(F(" ... ("));
    Serial.print(count);
    Serial.print(F(" bytes)"));
  }
  if (count > 0) {
    Serial.println();
  } else {
    Serial.print(F("[LD2450] "));
    Serial.print(label);
    Serial.println(F(" - keine Antwort (Baudrate/Verkabelung pruefen)"));
  }
  return count;
}

static bool containsSequence(const uint8_t *buf, size_t len, const uint8_t *seq, size_t seqLen) {
  for (size_t i = 0; i + seqLen <= len; i++) {
    if (memcmp(buf + i, seq, seqLen) == 0) return true;
  }
  return false;
}

// Forces multi-target tracking on every boot, and reports the firmware
// version so it can be checked without the HLKRadarTool phone app.
//
// Multi-target is the module's documented default, but it is a persisted
// setting: a factory reset, a firmware update, or a stray configuration from
// the phone app can leave it in SINGLE-target mode, where the module reports
// exactly one target no matter how many people are present. That looks
// identical to "the radar merged two people into one" from the Pi's side,
// and no amount of filtering downstream can recover the missing targets.
// Asserting it at boot costs one command and removes the whole failure mode.
//
// The saved zone is re-asserted into the module's registers in the same
// configuration session: they persist there too, but the two copies drifting
// apart (a zone edited while the XIAO was powered down, say) would be
// invisible and confusing. A second session straight after this one was
// ignored by the module (no reply to its enable-config).
static void sendZoneRegisters();
static void printZoneMode();

// Returns whether the module confirmed multi-target mode. If it did not
// answer at all (still booting, or a loose wire), loop() retries every
// CONFIG_RETRY_MS instead of running on in whatever mode it was left in.
static const unsigned long CONFIG_RETRY_MS = 10000;
static bool radarConfigured = false;
static unsigned long lastConfigAttemptMs = 0;

static bool configureRadar() {
  static const uint8_t CONF_VALUE[2] = {0x01, 0x00};
  static const uint8_t MULTI_CONFIRMED[5] = {0x91, 0x01, 0x00, 0x00, 0x02};
  uint8_t reply[48];

  sendConfigCommand(CMD_ENABLE_CONF, CONF_VALUE, sizeof(CONF_VALUE));
  drainConfigReply("enable-config");

  sendConfigCommand(CMD_QUERY_VERSION, nullptr, 0);
  drainConfigReply("firmware version");

  sendConfigCommand(CMD_MULTI_TARGET_MODE, nullptr, 0);
  drainConfigReply("multi-target mode");

  // Reply byte 10 is 0x01 for single-target, 0x02 for multi-target.
  sendConfigCommand(CMD_QUERY_TARGET_MODE, nullptr, 0);
  size_t n = drainConfigReply("target mode (02 = multi, 01 = single)", reply, sizeof(reply));
  bool multi = containsSequence(reply, n < sizeof(reply) ? n : sizeof(reply),
                                MULTI_CONFIRMED, sizeof(MULTI_CONFIRMED));

  sendZoneRegisters();

  sendConfigCommand(CMD_DISABLE_CONF, CONF_VALUE, sizeof(CONF_VALUE));
  drainConfigReply("end-config");

  if (multi) {
    Serial.println(F("[LD2450] Konfiguration abgeschlossen - multi-target mode confirmed"));
  } else {
    Serial.println(F("[LD2450] Multi-target mode NOT confirmed - retrying every 10 s"));
  }
  printZoneMode();
  return multi;
}

// Pushes the current zone rectangle into the module's own zone registers.
//
// Payload is 26 bytes: 2 for the zone type, then 3 zone slots of 4 int16
// coordinates each (x1, y1, x2, y2 - near-left corner then far-right).
// Only slot 1 is used here; the other two are left zeroed and ignored.
//
// NOTE the encoding asymmetry, which is easy to get wrong: incoming *data*
// frames encode coordinates with bit 15 as a sign flag (see decodeSigned),
// but this *command* takes plain two's-complement little-endian int16. Using
// decodeSigned's convention here would silently place the zone somewhere
// else entirely - mirrored across an axis for negative X.
static void sendZoneRegisters() {
  static const uint8_t CMD_SET_ZONE_REG = 0xC2;

  uint8_t payload[26] = {};
  payload[0] = zoneMode;
  payload[1] = 0x00;

  if (zoneMode != ZONE_MODE_SOFTWARE && zone.valid) {
    const int16_t coords[4] = {zone.minX, zone.minY, zone.maxX, zone.maxY};
    for (uint8_t i = 0; i < 4; i++) {
      uint16_t raw = (uint16_t)coords[i];
      payload[2 + i * 2] = raw & 0xFF;
      payload[2 + i * 2 + 1] = (raw >> 8) & 0xFF;
    }
  }

  sendConfigCommand(CMD_SET_ZONE_REG, payload, sizeof(payload));
  drainConfigReply("set-zone");
}

static void sendZoneToModule() {
  static const uint8_t CONF_VALUE[2] = {0x01, 0x00};

  sendConfigCommand(CMD_ENABLE_CONF, CONF_VALUE, sizeof(CONF_VALUE));
  drainConfigReply("enable-config");
  sendZoneRegisters();
  sendConfigCommand(CMD_DISABLE_CONF, CONF_VALUE, sizeof(CONF_VALUE));
  drainConfigReply("end-config");
  printZoneMode();
}

static void printZoneMode() {
  Serial.print(F("[Zone] Modus an Modul gesendet: "));
  if (zoneMode == ZONE_MODE_SOFTWARE) {
    Serial.println(F("SOFTWARE (Modul filtert nicht, alle Ziele sichtbar)"));
  } else if (zoneMode == ZONE_MODE_DETECTION) {
    Serial.println(F("DETECTION (Modul meldet nur Ziele INNERHALB der Zone)"));
  } else {
    Serial.println(F("FILTER (Modul meldet alles AUSSERHALB der Zone)"));
  }
}

static void setZoneMode(uint8_t mode) {
  if (mode > ZONE_MODE_FILTER) return;
  zoneMode = mode;
  saveZone();
  sendZoneToModule();
}

static const uint8_t FRAME_HEADER[4] = {0xAA, 0xFF, 0x03, 0x00};
static const uint8_t FRAME_FOOTER[2] = {0x55, 0xCC};
static const size_t FRAME_DATA_LEN = LD2450_MAX_TARGETS * 8; // 24 bytes
static const size_t FRAME_TOTAL_LEN =
    sizeof(FRAME_HEADER) + FRAME_DATA_LEN + sizeof(FRAME_FOOTER);

enum class ParseState { WAIT_HEADER, READ_BODY };

static ParseState parseState = ParseState::WAIT_HEADER;
static uint8_t frameBuf[FRAME_TOTAL_LEN];
static size_t frameBufLen = 0;

static RadarTarget targets[LD2450_MAX_TARGETS];
static bool haveFreshData = false;

// Ghost-target filter: the LD2450's own tracker "coasts" a lost target at
// its last known position for a while instead of clearing it immediately
// (avoids flicker on brief occlusions) - but on a longer disappearance
// (e.g. behind a wall) this can leave a stale target sitting there
// indefinitely, double-counting once the person actually returns.
//
// CAVEAT, and the reason the timeout below is deliberately generous: a
// coasted ghost and a person holding still are indistinguishable by position
// alone. Both report frozen coordinates and zero speed. This filter cannot
// actually tell them apart - it just assumes anything frozen is a ghost, and
// that assumption fails on exactly the person it matters most to see: someone
// standing quietly inside the zone, which is the privacy case this whole
// product exists to catch. ESPHome's ld2450 component, the most field-tested
// implementation of this sensor, has no equivalent filter at all; it treats
// still targets as first-class (speed == 0 means "still", not "absent").
//
// So: never drop a target the sensor reports as moving (below), and keep the
// timeout long enough that a briefly-motionless person survives it. Set
// STALE_TIMEOUT_MS to 0 to disable the filter entirely - worth trying once
// the module is on current firmware, whose static-target handling is much
// better than the version this was written against. Use
// scripts/check_stale_targets.py on the Pi to see which way it actually
// behaves in your room before tuning further.
static const int16_t STALE_NOISE_THRESHOLD_MM = 2;
static const unsigned long STALE_TIMEOUT_MS = 30000;
static int16_t lastSeenX[LD2450_MAX_TARGETS];
static int16_t lastSeenY[LD2450_MAX_TARGETS];
static unsigned long lastMovedMs[LD2450_MAX_TARGETS];

// How often the decoded targets get printed to the console. The sensor
// itself keeps sending frames much faster than this; we just skip printing
// most of them.
static const unsigned long PRINT_INTERVAL_MS = 1000;
static unsigned long lastPrintMs = 0;

// Keep-alive: a status packet goes out at least this often even without new
// frames, so the Pi can tell a silent radar module (framesPerSecond = 0 in
// the packet) from a dead radio link (no packets at all).
static const unsigned long STATUS_HEARTBEAT_MS = 1000;
static unsigned long lastStatusSendMs = 0;

// Forwarding cap. Every decoded frame used to be sent straight on, so the
// radio, the bridge, the Pi and every open browser tab all ran at whatever
// rate the module produced - 66+ frames/s were seen in one state. Presence
// and crossing detection need nothing near that; the newest state is sent
// at most this often.
static const unsigned long STATUS_MIN_INTERVAL_MS = 100;

// Targets are cleared once no valid frame has arrived for this long. Without
// it the keep-alive kept re-sending the last targets forever whenever the
// module stopped talking, and the Pi saw people who were long gone.
static const unsigned long FRAME_STALE_MS = 500;
static unsigned long lastFrameMs = 0;

static uint16_t framesThisSecond = 0, badFramesThisSecond = 0;
static uint8_t framesPerSecond = 0, badFramesPerSecond = 0;
static unsigned long statsWindowStartMs = 0;
static volatile uint16_t sendFailures = 0;

// The LD2450 encodes a signed coordinate/speed as: bit15 = sign
// (1 = positive, 0 = negative), bits 0-14 = magnitude.
static int16_t decodeSigned(uint8_t lowByte, uint8_t highByte) {
  uint16_t raw = (uint16_t)highByte << 8 | lowByte;
  bool positive = (raw & 0x8000) != 0;
  uint16_t magnitude = raw & 0x7FFF;
  return positive ? (int16_t)magnitude : -(int16_t)magnitude;
}

static void decodeFrame(const uint8_t *data) {
  // data points at the first byte after the 4-byte header, 24 bytes of
  // target data follow (3 targets x 8 bytes).
  for (int i = 0; i < LD2450_MAX_TARGETS; i++) {
    const uint8_t *t = data + (i * 8);
    int16_t x = decodeSigned(t[0], t[1]);
    int16_t y = decodeSigned(t[2], t[3]);
    // The module reports speed in cm/s (ESPHome's decode_speed multiplies by
    // 10 for mm/s). Passed on raw, every speed on the Pi was 10x too small.
    int32_t speedMms = (int32_t)decodeSigned(t[4], t[5]) * 10;
    int16_t speed = (int16_t)constrain(speedMms, (int32_t)-32767, (int32_t)32767);
    uint16_t resolution = (uint16_t)t[6] | ((uint16_t)t[7] << 8);

    // An all-zero block means the sensor has no target in that slot.
    bool active = !(x == 0 && y == 0 && speed == 0 && resolution == 0);

    if (active && calibrating) {
      calibrationHasData = true;
      calMinX = min(calMinX, x);
      calMaxX = max(calMaxX, x);
      calMinY = min(calMinY, y);
      calMaxY = max(calMaxY, y);
    }

    if (!active) {
      // Empty slot - reset its movement tracking so a new target here
      // later starts fresh rather than inheriting an old timestamp.
      lastMovedMs[i] = 0;
    } else {
      // speed != 0 is the sensor's own "this target is moving" signal (the
      // same test ESPHome uses to split still from moving targets). A target
      // it reports as moving is never a coasted ghost, so it must never be
      // filtered out no matter what its coordinates did.
      bool moved = lastMovedMs[i] == 0 || speed != 0 ||
                   abs(x - lastSeenX[i]) > STALE_NOISE_THRESHOLD_MM ||
                   abs(y - lastSeenY[i]) > STALE_NOISE_THRESHOLD_MM;
      if (moved) {
        lastSeenX[i] = x;
        lastSeenY[i] = y;
        lastMovedMs[i] = millis();
      } else if (STALE_TIMEOUT_MS > 0 && millis() - lastMovedMs[i] > STALE_TIMEOUT_MS) {
        // Frozen in place too long - almost certainly a coasted/ghost
        // track, not a real person. Stop counting it.
        active = false;
      }
    }

    // In SOFTWARE mode raw hardware detections are always reported (so the
    // settings page can show every tracked target, e.g. to check the sensor
    // is really seeing 3 people at all) and the zone only marks which ones
    // count for presence.
    //
    // In DETECTION/FILTER mode the module has already applied the rectangle
    // upstream, so anything that still arrives here passed its test by
    // definition - re-applying isInsideZone() would be wrong in both
    // directions: redundant under DETECTION, and exactly inverted under
    // FILTER, where every reported target is deliberately outside the box.
    bool insideZone = (zoneMode != ZONE_MODE_SOFTWARE) || !zone.valid || isInsideZone(x, y);

    targets[i].active = active;
    targets[i].insideZone = insideZone;
    targets[i].x_mm = x;
    targets[i].y_mm = y;
    targets[i].speed_mms = speed;
    targets[i].resolution_mm = resolution;
  }
}

static void printTargets() {
  int count = 0;
  for (int i = 0; i < LD2450_MAX_TARGETS; i++) {
    if (targets[i].active) count++;
  }

  Serial.println();
  Serial.print(F("People detected: "));
  Serial.println(count);

  Serial.print(F("[Stats] "));
  Serial.print(framesPerSecond);
  Serial.print(F(" frames/s, "));
  Serial.print(badFramesPerSecond);
  Serial.print(F(" bad frames/s, "));
  Serial.print(sendFailures);
  Serial.print(F(" radio send failures so far, last frame "));
  if (lastFrameMs == 0) {
    Serial.println(F("never"));
  } else {
    Serial.print(millis() - lastFrameMs);
    Serial.println(F(" ms ago"));
  }

  if (count == 0) {
    Serial.println(F("(no targets in range)"));
    return;
  }

  for (int i = 0; i < LD2450_MAX_TARGETS; i++) {
    if (!targets[i].active) continue;

    float distance =
        sqrtf((float)targets[i].x_mm * targets[i].x_mm +
              (float)targets[i].y_mm * targets[i].y_mm);

    Serial.println();
    Serial.print(F("Target "));
    Serial.print(i + 1);
    Serial.println(F(":"));
    Serial.print(F("X: "));
    Serial.print(targets[i].x_mm);
    Serial.println(F(" mm"));
    Serial.print(F("Y: "));
    Serial.print(targets[i].y_mm);
    Serial.println(F(" mm"));
    Serial.print(F("Distance: "));
    Serial.print((int)distance);
    Serial.println(F(" mm"));
    Serial.print(F("Speed: "));
    Serial.print(targets[i].speed_mms);
    Serial.println(F(" mm/s"));
  }
}

// ---------------------------------------------------------------------------
// ESP-NOW wire protocol shared with mmWaveBridge/src/main.cpp. Only two
// devices are involved, so both just use the broadcast address instead of
// pairing/hardcoding MAC addresses.
// ---------------------------------------------------------------------------
struct __attribute__((packed)) TargetWire {
  uint8_t active;
  uint8_t insideZone;
  int16_t x_mm;
  int16_t y_mm;
  int16_t speed_mms;
  uint16_t resolution_mm;
};

// msgType 2 = this layout, with the diagnostics block appended. The bridge
// still accepts the shorter msgType 1 packet of the previous firmware, so the
// two boards can be updated one at a time (bridge first).
struct __attribute__((packed)) StatusPacket {
  uint8_t msgType;
  TargetWire targets[LD2450_MAX_TARGETS];
  uint8_t zoneValid;
  int16_t zoneMinX, zoneMaxX, zoneMinY, zoneMaxY;
  uint8_t calibrating;
  uint32_t calibrationRemainingMs;
  uint8_t zoneMode;
  uint8_t framesPerSecond;
  uint8_t badFramesPerSecond;
  uint16_t sendFailures;
  uint16_t frameAgeMs;
};

enum CommandType : uint8_t {
  CMD_SET_ZONE = 10,
  CMD_RESET_ZONE = 11,
  CMD_START_CALIBRATION = 12,
  CMD_SET_ZONE_MODE = 13,
};

struct __attribute__((packed)) CommandPacket {
  uint8_t msgType;
  int16_t minX, maxX, minY, maxY;
  uint32_t durationMs;
  uint8_t zoneMode;
};

static void sendStatusPacket() {
  StatusPacket packet;
  packet.msgType = 2;
  for (int i = 0; i < LD2450_MAX_TARGETS; i++) {
    packet.targets[i].active = targets[i].active ? 1 : 0;
    packet.targets[i].insideZone = targets[i].insideZone ? 1 : 0;
    packet.targets[i].x_mm = targets[i].x_mm;
    packet.targets[i].y_mm = targets[i].y_mm;
    packet.targets[i].speed_mms = targets[i].speed_mms;
    packet.targets[i].resolution_mm = targets[i].resolution_mm;
  }
  packet.zoneValid = zone.valid ? 1 : 0;
  packet.zoneMinX = zone.minX;
  packet.zoneMaxX = zone.maxX;
  packet.zoneMinY = zone.minY;
  packet.zoneMaxY = zone.maxY;
  packet.calibrating = calibrating ? 1 : 0;
  packet.calibrationRemainingMs =
      calibrating && calibrationEndMs > millis() ? (calibrationEndMs - millis()) : 0;
  packet.zoneMode = zoneMode;
  packet.framesPerSecond = framesPerSecond;
  packet.badFramesPerSecond = badFramesPerSecond;
  packet.sendFailures = sendFailures;
  unsigned long age = lastFrameMs == 0 ? 65535UL : millis() - lastFrameMs;
  packet.frameAgeMs = (uint16_t)(age > 65535UL ? 65535UL : age);

  if (esp_now_send(PEER_MAC, (const uint8_t *)&packet, sizeof(packet)) != ESP_OK) {
    sendFailures++;
  }
}

static void onDataSent(const uint8_t *macAddr, esp_now_send_status_t status) {
  if (status != ESP_NOW_SEND_SUCCESS) sendFailures++;
}

// The ESP-NOW receive callback runs in the WiFi task. Zone commands used to be
// executed right here: pushing a zone to the module blocks on the UART for up
// to seconds (and, before the drain cap, forever), which stalled the radio
// and raced loop() for the same UART. The callback now only queues the
// command; loop() carries it out.
static QueueHandle_t commandQueue = nullptr;

static void onCommandReceived(const uint8_t *macAddr, const uint8_t *data, int len) {
  if (len != sizeof(CommandPacket) || commandQueue == nullptr) return;
  xQueueSend(commandQueue, data, 0);
}

static void processCommands() {
  CommandPacket cmd;
  while (commandQueue != nullptr && xQueueReceive(commandQueue, &cmd, 0) == pdTRUE) {
    switch (cmd.msgType) {
      case CMD_SET_ZONE:
        setZoneDirect(cmd.minX, cmd.maxX, cmd.minY, cmd.maxY);
        break;
      case CMD_RESET_ZONE:
        resetZone();
        break;
      case CMD_START_CALIBRATION:
        startCalibration(cmd.durationMs > 0 ? cmd.durationMs : CALIBRATION_DEFAULT_MS);
        break;
      case CMD_SET_ZONE_MODE:
        setZoneMode(cmd.zoneMode);
        break;
    }
  }
}

static void setupEspNow() {
  WiFi.mode(WIFI_STA);

  // Disable WiFi modem sleep. It makes current draw dip periodically,
  // which some powerbanks read as "nothing plugged in" and shut off -
  // ESP-NOW's short burst transmissions (vs. a continuously active HTTP
  // server) make this more likely to trip than the old WiFi/HTTP setup.
  WiFi.setSleep(false);

  if (esp_now_init() != ESP_OK) {
    Serial.println(F("[ESP-NOW] Init failed"));
    return;
  }

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, PEER_MAC, 6);
  peer.channel = 0;
  peer.encrypt = false;
  if (esp_now_add_peer(&peer) != ESP_OK) {
    Serial.println(F("[ESP-NOW] Failed to add bridge peer"));
  }

  commandQueue = xQueueCreate(4, sizeof(CommandPacket));
  esp_now_register_recv_cb(onCommandReceived);
  esp_now_register_send_cb(onDataSent);
  Serial.println(F("[ESP-NOW] Ready, broadcasting status packets"));
  Serial.print(F("[ESP-NOW] This device's MAC: "));
  Serial.println(WiFi.macAddress());
}

// Feed one byte at a time into the frame state machine. Non-blocking:
// only ever consumes bytes that are already available in the UART buffer.
static void handleByte(uint8_t b) {
  switch (parseState) {
    case ParseState::WAIT_HEADER: {
      // Shift a rolling window and compare against the 4-byte header.
      if (frameBufLen < sizeof(FRAME_HEADER)) {
        frameBuf[frameBufLen++] = b;
        if (frameBufLen == sizeof(FRAME_HEADER) &&
            memcmp(frameBuf, FRAME_HEADER, sizeof(FRAME_HEADER)) != 0) {
          // Not a real header - slide the window by one byte and retry.
          memmove(frameBuf, frameBuf + 1, sizeof(FRAME_HEADER) - 1);
          frameBufLen = sizeof(FRAME_HEADER) - 1;
        } else if (frameBufLen == sizeof(FRAME_HEADER)) {
          parseState = ParseState::READ_BODY;
        }
      }
      break;
    }
    case ParseState::READ_BODY: {
      frameBuf[frameBufLen++] = b;
      if (frameBufLen == FRAME_TOTAL_LEN) {
        const uint8_t *footer = frameBuf + sizeof(FRAME_HEADER) + FRAME_DATA_LEN;
        if (memcmp(footer, FRAME_FOOTER, sizeof(FRAME_FOOTER)) == 0) {
          decodeFrame(frameBuf + sizeof(FRAME_HEADER));
          haveFreshData = true;
          lastFrameMs = millis();
          framesThisSecond++;
        } else {
          badFramesThisSecond++;
        }
        // Reset for the next frame.
        parseState = ParseState::WAIT_HEADER;
        frameBufLen = 0;
      }
      break;
    }
  }
}

static void processRadar() {
  while (ld2450Serial.available() > 0) {
    handleByte((uint8_t)ld2450Serial.read());
  }
}

// Lets you trigger calibration from the serial monitor: send 'c' to start
// a calibration walk, 'r' to clear the saved zone.
static void handleSerialCommands() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == 'c') {
      startCalibration(CALIBRATION_DEFAULT_MS);
    } else if (c == 'r') {
      resetZone();
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println(F("LD2450 mmWave radar - room occupancy sensor"));
  Serial.println(F("Coordinate system: X- = left, X+ = right, Y+ = in front of sensor"));

  // The default 256-byte receive buffer holds well under a second of frames;
  // any pause in loop() (a zone push, NVS write) overflowed it and corrupted
  // the frame stream.
  ld2450Serial.setRxBufferSize(2048);
  ld2450Serial.begin(LD2450_BAUD, SERIAL_8N1, LD2450_RX_PIN, LD2450_TX_PIN);
  Serial.println(F("[LD2450] UART2 initialized (256000 8N1, RX=44, TX=43)"));

  loadZone();
  if (zone.valid) {
    Serial.println(F("[Calibration] Loaded saved room zone"));
  } else {
    Serial.println(F("[Calibration] No saved zone - tracking is unfiltered"));
  }

  delay(500);  // let the module finish its own boot before configuring it
  radarConfigured = configureRadar();
  lastConfigAttemptMs = millis();
  Serial.println(F("[Calibration] Send 'c' to start a calibration walk, 'r' to reset the zone"));

  setupEspNow();
  statsWindowStartMs = millis();
}

static void updateStats(unsigned long now) {
  if (now - statsWindowStartMs < 1000) return;
  framesPerSecond = framesThisSecond > 255 ? 255 : framesThisSecond;
  badFramesPerSecond = badFramesThisSecond > 255 ? 255 : badFramesThisSecond;
  framesThisSecond = 0;
  badFramesThisSecond = 0;
  statsWindowStartMs = now;
}

static void expireStaleTargets(unsigned long now) {
  if (lastFrameMs == 0 || now - lastFrameMs <= FRAME_STALE_MS) return;
  bool cleared = false;
  for (int i = 0; i < LD2450_MAX_TARGETS; i++) {
    if (targets[i].active) {
      targets[i].active = false;
      lastMovedMs[i] = 0;
      cleared = true;
    }
  }
  if (cleared) haveFreshData = true;
}

void loop() {
  processRadar();
  handleSerialCommands();
  processCommands();

  if (calibrating && millis() >= calibrationEndMs) {
    finishCalibration();
  }

  if (!radarConfigured && millis() - lastConfigAttemptMs >= CONFIG_RETRY_MS) {
    radarConfigured = configureRadar();
    lastConfigAttemptMs = millis();
  }

  unsigned long now = millis();
  updateStats(now);
  expireStaleTargets(now);

  bool fresh = haveFreshData && now - lastStatusSendMs >= STATUS_MIN_INTERVAL_MS;
  if (fresh || now - lastStatusSendMs >= STATUS_HEARTBEAT_MS) {
    sendStatusPacket();
    haveFreshData = false;
    lastStatusSendMs = now;
  }

  // Print at a fixed, human-readable rate instead of on every frame - the
  // status packet above is still sent on every frame, only this console
  // output is throttled.
  if (now - lastPrintMs >= PRINT_INTERVAL_MS) {
    printTargets();
    lastPrintMs = now;
  }
}
