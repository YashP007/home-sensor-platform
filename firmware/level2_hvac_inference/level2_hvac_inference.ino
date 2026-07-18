/*
 * SmartHome Monitor — Level 2: HVAC State Inference
 *
 * Extends Level 1 with rolling temperature slope regression to detect
 * when the HVAC system (furnace in winter, AC in summer) transitions
 * between ON and OFF states.
 *
 * Algorithm:
 *   1. Sample BME280 every 5 seconds.
 *   2. Apply exponential smoothing (EMA) to reduce measurement noise.
 *   3. Maintain a 60-second rolling buffer of smoothed temperatures.
 *   4. Compute the slope (°F/min) between the oldest and newest samples
 *      in the buffer.
 *   5. Compare the slope against ON/OFF thresholds appropriate for the
 *      current season mode.
 *   6. Require N consecutive slope confirmations before declaring a
 *      state transition (hysteresis).
 *   7. Use a "dead zone" hold mechanism to prevent counter resets during
 *      brief flat periods that would otherwise miss valid transitions.
 *
 * Design note:
 *   The original implementation of this algorithm ran on a DHT11 sensor,
 *   whose ±2°C accuracy and 1°C resolution required the dead-zone hold
 *   mechanism to compensate for staircase-shaped temperature traces.
 *   The BME280's 0.01°C resolution makes this less critical, but the
 *   hold mechanism is preserved because it also handles the flat portions
 *   between HVAC cycles when the room reaches thermal equilibrium.
 *
 * Published feeds (in addition to Level 1):
 *   - home-hvac-state: "HEAT-ON" | "HEAT-OFF" | "AC-ON" | "AC-OFF"
 */

#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>
#include "AdafruitIO_WiFi.h"
#include "secrets.h"

// ─── Season mode ────────────────────────────────────────────────────────
// 0 = WINTER (detect furnace — temperature RISES when heat is on)
// 1 = SUMMER (detect AC — temperature FALLS when AC is on)
constexpr uint8_t SEASON_MODE = 0;

// ─── Configuration ──────────────────────────────────────────────────────
constexpr uint8_t   BME280_I2C_ADDR      = 0x77;
constexpr uint32_t  SAMPLE_INTERVAL_MS   = 5000;
constexpr uint32_t  PUBLISH_INTERVAL_MS  = 30000;

// Slope thresholds (°F/min), derived from historical calibration data.
constexpr float SLOPE_ON_F_PER_MIN   = 0.25f;   // clear ON signal
constexpr float SLOPE_OFF_F_PER_MIN  = 0.05f;   // clear OFF signal
constexpr float SLOPE_DEADZONE       = 0.20f;   // ambiguity band

// Confirmation counts before state transition. Prevents single-sample noise.
constexpr uint8_t CONFIRM_SAMPLES = 3;
constexpr uint8_t HOLD_SAMPLES    = 5;   // hold during dead-zone ambiguity

// Temperature history buffer for slope regression.
constexpr uint8_t  HIST_LEN         = 16;   // 16 * 5s = 80s of history
constexpr uint32_t SLOPE_WINDOW_MS  = 60000;
constexpr float    EMA_ALPHA        = 0.25f;

// ─── State ──────────────────────────────────────────────────────────────
Adafruit_BME280 bme;
AdafruitIO_WiFi io(IO_USERNAME, IO_KEY, WIFI_SSID, WIFI_PASS);
AdafruitIO_Feed *feedTemp = io.feed(IO_FEED_TEMP);
AdafruitIO_Feed *feedHvac = io.feed(IO_FEED_HVAC);

struct TempSample { uint32_t ms; float tempF; };
TempSample history[HIST_LEN] = {};
uint8_t  histCount = 0;
uint8_t  histHead  = 0;

float    emaTempF = NAN;
bool     hvacOn   = false;
uint8_t  onConfirm = 0, offConfirm = 0;
uint8_t  onHold    = 0, offHold    = 0;

uint32_t lastSampleMs  = 0;
uint32_t lastPublishMs = 0;
uint32_t cycleCount    = 0;    // total HVAC ON transitions since boot

// ─── History buffer helpers ─────────────────────────────────────────────

void pushSample(uint32_t nowMs, float tempF) {
  history[histHead] = {nowMs, tempF};
  histHead = (histHead + 1) % HIST_LEN;
  if (histCount < HIST_LEN) histCount++;
}

// Return true if we found a sample >= SLOPE_WINDOW_MS old, populating outSample.
bool findOldestInWindow(uint32_t nowMs, TempSample &outSample) {
  if (histCount < 2) return false;
  for (uint8_t i = 0; i < histCount; i++) {
    uint8_t idx = (histHead + HIST_LEN - 1 - i) % HIST_LEN;
    if (nowMs - history[idx].ms >= SLOPE_WINDOW_MS) {
      outSample = history[idx];
      return true;
    }
  }
  return false;
}

// Slope in °F/min between the oldest in-window sample and the newest.
float computeSlopeFPerMin(uint32_t nowMs, float currentTempF) {
  TempSample oldest;
  if (!findOldestInWindow(nowMs, oldest)) return NAN;
  float deltaMinutes = (nowMs - oldest.ms) / 60000.0f;
  if (deltaMinutes < 0.01f) return NAN;
  return (currentTempF - oldest.tempF) / deltaMinutes;
}

// ─── HVAC state machine ─────────────────────────────────────────────────

void updateHvacState(uint32_t nowMs, float tempF, float slope) {
  if (isnan(slope)) return;

  float absSlope    = fabs(slope);
  bool  inDeadZone  = (absSlope < SLOPE_DEADZONE);
  bool  strongOn    = false;
  bool  strongOff   = false;

  if (SEASON_MODE == 0) {              // WINTER — heat rising means ON
    strongOn  = (slope >  SLOPE_ON_F_PER_MIN);
    strongOff = (slope < -SLOPE_OFF_F_PER_MIN);
  } else {                              // SUMMER — heat falling means AC ON
    strongOn  = (slope < -SLOPE_ON_F_PER_MIN);
    strongOff = (slope >  SLOPE_OFF_F_PER_MIN);
  }

  if (!hvacOn) {
    // Currently OFF, watching for ON transition
    if      (strongOn)                   { onConfirm++; onHold = HOLD_SAMPLES; }
    else if (inDeadZone && onHold > 0)   { onHold--; }
    else if (strongOff)                  { onConfirm = 0; onHold = 0; }
    else if (!inDeadZone)                { onConfirm = 0; onHold = 0; }
    // else: ambiguous with no hold — wait without resetting

    if (onConfirm >= CONFIRM_SAMPLES) {
      hvacOn = true;
      onConfirm = offConfirm = 0;
      onHold = offHold = 0;
      cycleCount++;
      Serial.printf("[HVAC] %s ON @ %.1fF  slope=%+.2fF/min  cycle #%lu\n",
                    (SEASON_MODE == 0) ? "HEAT" : "AC ",
                    tempF, slope, cycleCount);
      publishHvacState(true);
    }
  } else {
    // Currently ON, watching for OFF transition
    if      (strongOff)                  { offConfirm++; offHold = HOLD_SAMPLES; }
    else if (inDeadZone && offHold > 0)  { offHold--; }
    else if (strongOn)                   { offConfirm = 0; offHold = 0; }
    else if (!inDeadZone)                { offConfirm = 0; offHold = 0; }

    if (offConfirm >= CONFIRM_SAMPLES) {
      hvacOn = false;
      onConfirm = offConfirm = 0;
      onHold = offHold = 0;
      Serial.printf("[HVAC] %s OFF @ %.1fF  slope=%+.2fF/min\n",
                    (SEASON_MODE == 0) ? "HEAT" : "AC ",
                    tempF, slope);
      publishHvacState(false);
    }
  }
}

void publishHvacState(bool on) {
  const char *state;
  if (SEASON_MODE == 0) state = on ? "HEAT-ON" : "HEAT-OFF";
  else                  state = on ? "AC-ON"   : "AC-OFF";
  if (!feedHvac->save(state)) {
    Serial.println("[IO]   ERROR: hvac state publish failed.");
  }
}

// ─── Setup ──────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(2000);
  Serial.println();
  Serial.println("[BOOT] SmartHome Monitor Level 2 — HVAC State Inference");
  Serial.printf ("[BOOT] Season mode: %s\n", (SEASON_MODE == 0) ? "WINTER" : "SUMMER");

  Wire.begin();
  if (!bme.begin(BME280_I2C_ADDR, &Wire) && !bme.begin(0x76, &Wire)) {
    Serial.println("[BME]  ERROR: sensor not found. Halting.");
    while (1) delay(1000);
  }
  bme.setSampling(Adafruit_BME280::MODE_NORMAL,
                  Adafruit_BME280::SAMPLING_X2,
                  Adafruit_BME280::SAMPLING_X16,
                  Adafruit_BME280::SAMPLING_X1,
                  Adafruit_BME280::FILTER_X16,
                  Adafruit_BME280::STANDBY_MS_500);
  Serial.println("[BME]  Sensor initialized.");

  Serial.print("[WIFI] Connecting");
  io.connect();
  while (io.status() < AIO_CONNECTED) { Serial.print("."); delay(500); }
  Serial.printf("\n[IO]   %s\n", io.statusText());
}

// ─── Main loop ──────────────────────────────────────────────────────────

void loop() {
  io.run();

  uint32_t now = millis();
  if (now - lastSampleMs < SAMPLE_INTERVAL_MS) return;
  lastSampleMs = now;

  float tempC = bme.readTemperature();
  float tempF = tempC * 9.0f / 5.0f + 32.0f;

  // Apply exponential smoothing.
  emaTempF = isnan(emaTempF) ? tempF : (EMA_ALPHA * tempF + (1.0f - EMA_ALPHA) * emaTempF);

  pushSample(now, emaTempF);
  float slope = computeSlopeFPerMin(now, emaTempF);
  updateHvacState(now, emaTempF, slope);

  // Periodic publish of temperature.
  if (now - lastPublishMs >= PUBLISH_INTERVAL_MS) {
    lastPublishMs = now;
    if (feedTemp->save(emaTempF)) {
      Serial.printf("[LOOP] T=%.2fF  slope=%+.2fF/min  hvac=%s  confirm=%u\n",
                    emaTempF,
                    isnan(slope) ? 0.0f : slope,
                    hvacOn ? "ON" : "OFF",
                    hvacOn ? offConfirm : onConfirm);
    }
  }
}
