/*
 * SmartHome Monitor — Level 2: HVAC State Inference
 *
 * Detects when the HVAC system (furnace in winter, AC in summer) transitions
 * between ON and OFF by regressing the slope of a rolling temperature window.
 *
 * Algorithm:
 *   1. Sample BME280 every 5 seconds.
 *   2. Apply exponential smoothing (EMA) to reduce measurement noise.
 *   3. Maintain a rolling buffer of smoothed temperatures.
 *   4. Least-squares regress the slope (°F/min) over the trailing window.
 *   5. Feed the slope into a Schmitt trigger: separate ENTER and EXIT
 *      thresholds give hysteresis without any ad-hoc hold logic.
 *   6. Require N consecutive confirmations before declaring a transition, and
 *      enforce a minimum dwell time to prevent short-cycling.
 *   7. Re-publish the current state periodically so the feed can never sit
 *      stale after a missed edge.
 *
 * Published feeds (in addition to Level 1):
 *   - home-hvac-state: "HEAT-ON" | "HEAT-OFF" | "AC-ON" | "AC-OFF"
 *   - home-humidity:   %RH, EMA-smoothed, published on >= HUMIDITY_CHANGE_THRESHOLD_RH
 *                       move or on the staleness heartbeat.
 *   - home-pressure:   hPa, EMA-smoothed, published on >= PRESSURE_CHANGE_THRESHOLD_HPA
 *                       move or on the staleness heartbeat.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * REVISION NOTE — 2026-08-03: added change-triggered humidity and pressure
 * publishing (dashboard showed home-humidity/home-pressure stuck at
 * 3-days-old values — Level 1 never republished them at all).
 *
 *   - Both channels are EMA-smoothed with the same alpha as temperature, so
 *     a single noisy BME280 sample can't fire a spurious publish.
 *   - Humidity threshold: +/-2 %RH. The BME280's own accuracy spec is
 *     +/-3 %RH, so anything tighter would mostly publish sensor noise. 2 %RH
 *     is small enough to catch plant-relevant swings (post-watering,
 *     a window opening in a poorly-sealed apartment) without spamming the
 *     feed on ordinary sample-to-sample wobble.
 *   - Pressure threshold: +/-2 hPa, as specified.
 *   - A staleness heartbeat (STALE_PUBLISH_MS, default 1 hour) force-publishes
 *     both channels even with no qualifying change, matching the pattern
 *     already used for home-hvac-state, so a genuinely flat channel doesn't
 *     look dead on the dashboard. Adjust STALE_PUBLISH_MS if 1 hour isn't
 *     the cadence you want.
 *   - Assumes secrets.h defines IO_FEED_HUMIDITY and IO_FEED_PRESSURE
 *     (same pattern as the existing IO_FEED_TEMP / IO_FEED_HVAC), matching
 *     the home-humidity / home-pressure feed keys on your IO dashboard.
 *     Add them if not already present:
 *       #define IO_FEED_HUMIDITY "home-humidity"
 *       #define IO_FEED_PRESSURE "home-pressure"
 * ─────────────────────────────────────────────────────────────────────────
 *
 * ─────────────────────────────────────────────────────────────────────────
 * REVISION NOTE — 2026-07-29: four defects fixed after the state feed was
 * observed pinned at 1 for two days straight. See docs/HVAC_STATE_BUG.md.
 *
 *   1. PUBLISH BUG (the reason the feed never changed). publishHvacState()
 *      passed a `const char *` to AdafruitIO_Feed::save(). There is no
 *      const-qualified char* overload, so overload resolution fell through to
 *      save(bool) via the standard pointer→bool conversion. Every call passed
 *      a non-null pointer, so EVERY publish wrote 1 — for ON and for OFF
 *      alike. The state machine was transitioning correctly the whole time;
 *      only the transport was broken. Now published through a mutable buffer
 *      that binds unambiguously to save(char *).
 *
 *   2. SEASON_MODE was left at 0 (WINTER) while collecting July AC data, so
 *      even a correct publish would have emitted inverted "HEAT-*" labels.
 *
 *   3. OFF THRESHOLD was unreachable in practice. It required the room to be
 *      WARMING at > +0.05 °F/min before declaring the AC off. Measured
 *      recovery rates in this apartment are +0.06 °F/min (evening) and
 *      +0.01–0.03 °F/min (night) — at or below the threshold. The exit test
 *      is now "cooling has stopped" (slope risen above −0.08 °F/min), which is
 *      what actually distinguishes an idle compressor from a running one.
 *
 *   4. DEAD-ZONE HOLD removed. With SLOPE_DEADZONE (0.20) four times wider
 *      than SLOPE_OFF (0.05), slopes in the 0.05–0.20 band satisfied both
 *      `strongOff` and `inDeadZone`, so behaviour depended entirely on the
 *      order of the else-if chain and differed between the ON and OFF
 *      branches. Two clean Schmitt thresholds do the same job unambiguously.
 *      (The hold existed to paper over DHT11 staircase quantisation; the
 *      BME280 at 0.01 °C resolution does not need it.)
 *
 * Thresholds below are calibrated against collected-data/, 2026-07-27 →
 * 2026-07-29: 17 AC cycles measured, cooling −0.15 to −0.87 °F/min
 * (median −0.60), recovery +0.01 to +0.31 °F/min. Re-derive with
 * collected-data/analyze_temperature.py after any seasonal change.
 * ─────────────────────────────────────────────────────────────────────────
 */

#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>
#include "AdafruitIO_WiFi.h"
#include "secrets.h"

// ─── Season mode ────────────────────────────────────────────────────────
// 0 = WINTER (detect furnace — temperature RISES when heat is on)
// 1 = SUMMER (detect AC in summer — temperature FALLS when AC is on)
//
// *** THIS MUST MATCH THE SEASON YOU ARE ACTUALLY RECORDING IN. ***
// Getting it wrong does not fail loudly; it silently inverts every label.
constexpr uint8_t SEASON_MODE = 1;      // SUMMER — AC detection

// ─── Configuration ──────────────────────────────────────────────────────
constexpr uint8_t   BME280_I2C_ADDR      = 0x77;
constexpr uint32_t  SAMPLE_INTERVAL_MS   = 5000;
constexpr uint32_t  PUBLISH_INTERVAL_MS  = 30000;

// Re-publish the current HVAC state even when nothing changed, so a missed
// edge cannot leave the dashboard stale indefinitely.
constexpr uint32_t  STATE_HEARTBEAT_MS   = 300000;   // 5 min

// Same idea, applied to humidity/pressure: force a publish at this interval
// even if neither channel has moved enough to qualify on its own.
constexpr uint32_t  STALE_PUBLISH_MS     = 3600000;  // 1 hour

// ─── Slope thresholds (°F/min) — Schmitt trigger ────────────────────────
// ENTER: unambiguously running.  EXIT: unambiguously not running.
// The gap between them IS the hysteresis; nothing else is needed.
//
//   measured running   : 0.15 – 0.87 °F/min   (median 0.60)
//   measured idle drift: 0.01 – 0.31 °F/min   (median 0.05)
//
// ENTER sits above the idle drift median with margin; EXIT sits below the
// slowest observed compressor rate. Widen ENTER if you see false positives
// during the fast post-cycle recovery.
constexpr float SLOPE_ENTER_F_PER_MIN = 0.25f;   // magnitude, signed by season
constexpr float SLOPE_EXIT_F_PER_MIN  = 0.08f;   // magnitude, signed by season

// Confirmation counts before a transition. At 5 s/sample, 3 = 15 s.
constexpr uint8_t  CONFIRM_SAMPLES  = 3;
// Minimum time in a state before the opposite transition may fire. Real
// compressors do not cycle faster than this; anything that does is noise.
constexpr uint32_t MIN_STATE_HOLD_MS = 90000;    // 90 s

// ─── Change-triggered publish thresholds ─────────────────────────────────
// A publish fires when the EMA-smoothed reading has moved this far from the
// last *published* value. Set below sensor accuracy and you just publish
// noise; set well above it and you lose real signal.
//
//   BME280 humidity accuracy: +/-3 %RH  -> 2 %RH threshold stays under that,
//     while still filtering sample-to-sample jitter.
//   BME280 pressure accuracy: +/-1 hPa  -> 2 hPa threshold as specified.
constexpr float HUMIDITY_CHANGE_THRESHOLD_RH  = 2.0f;   // %RH
constexpr float PRESSURE_CHANGE_THRESHOLD_HPA = 2.0f;   // hPa

// ─── Temperature history buffer for slope regression ────────────────────
constexpr uint8_t  HIST_LEN         = 16;   // 16 * 5s = 80s of history
constexpr uint32_t SLOPE_WINDOW_MS  = 60000;
constexpr uint8_t  MIN_SLOPE_POINTS = 6;    // need this many in-window samples
constexpr float    EMA_ALPHA        = 0.25f;

// ─── State ──────────────────────────────────────────────────────────────
Adafruit_BME280 bme;
AdafruitIO_WiFi io(IO_USERNAME, IO_KEY, WIFI_SSID, WIFI_PASS);
AdafruitIO_Feed *feedTemp     = io.feed(IO_FEED_TEMP);
AdafruitIO_Feed *feedHvac     = io.feed(IO_FEED_HVAC);
AdafruitIO_Feed *feedHumidity = io.feed(IO_FEED_HUMID);
AdafruitIO_Feed *feedPressure = io.feed(IO_FEED_PRESS);

struct TempSample { uint32_t ms; float tempF; };
TempSample history[HIST_LEN] = {};
uint8_t  histCount = 0;
uint8_t  histHead  = 0;

float    emaTempF = NAN;
bool     hvacOn   = false;
uint8_t  onConfirm = 0, offConfirm = 0;

// EMA-smoothed humidity/pressure, and the last value actually published for
// each — publish decisions are "distance since last publish", not
// "distance since last sample".
float    emaHumidityRh     = NAN;
float    emaPressureHpa    = NAN;
float    lastPubHumidityRh  = NAN;
float    lastPubPressureHpa = NAN;

uint32_t lastSampleMs      = 0;
uint32_t lastPublishMs     = 0;
uint32_t lastStateChangeMs = 0;
uint32_t lastStatePubMs    = 0;
uint32_t lastHumidityPubMs = 0;
uint32_t lastPressurePubMs = 0;
uint32_t cycleCount        = 0;    // total HVAC ON transitions since boot

// Forward declaration — Arduino's auto-prototyping usually covers this, but
// relying on it is fragile once the file grows or gets split.
void publishHvacState(bool on);

// ─── History buffer helpers ─────────────────────────────────────────────

void pushSample(uint32_t nowMs, float tempF) {
  history[histHead] = {nowMs, tempF};
  histHead = (histHead + 1) % HIST_LEN;
  if (histCount < HIST_LEN) histCount++;
}

/*
 * Least-squares slope in °F/min over every sample inside SLOPE_WINDOW_MS.
 *
 * The previous implementation differenced only the two endpoints, which puts
 * the entire estimate at the mercy of noise on exactly two samples. A
 * regression over the ~12 in-window samples cuts the slope variance by
 * roughly the sample count and costs nothing at this rate.
 *
 * Returns NAN when there is not enough history to be meaningful.
 */
float computeSlopeFPerMin(uint32_t nowMs) {
  if (histCount < MIN_SLOPE_POINTS) return NAN;

  float sumT = 0, sumY = 0, sumTT = 0, sumTY = 0;
  uint8_t n = 0;

  for (uint8_t i = 0; i < histCount; i++) {
    uint8_t idx = (histHead + HIST_LEN - 1 - i) % HIST_LEN;
    uint32_t age = nowMs - history[idx].ms;      // unsigned: rollover-safe
    if (age > SLOPE_WINDOW_MS) continue;

    float t = -(float)age / 60000.0f;            // minutes, negative into past
    float y = history[idx].tempF;
    sumT += t;  sumY += y;  sumTT += t * t;  sumTY += t * y;
    n++;
  }

  if (n < MIN_SLOPE_POINTS) return NAN;

  float denom = (float)n * sumTT - sumT * sumT;
  if (fabs(denom) < 1e-6f) return NAN;
  return ((float)n * sumTY - sumT * sumY) / denom;
}

// ─── HVAC state machine ─────────────────────────────────────────────────

void updateHvacState(uint32_t nowMs, float tempF, float slope) {
  if (isnan(slope)) return;

  // Sign convention: in SUMMER the AC drives temperature DOWN, so both
  // thresholds are negated. Everything downstream is season-agnostic.
  bool running, idle;
  if (SEASON_MODE == 0) {              // WINTER — furnace drives temp up
    running = (slope >  SLOPE_ENTER_F_PER_MIN);
    idle    = (slope <  SLOPE_EXIT_F_PER_MIN);
  } else {                             // SUMMER — AC drives temp down
    running = (slope < -SLOPE_ENTER_F_PER_MIN);
    idle    = (slope > -SLOPE_EXIT_F_PER_MIN);
  }

  bool dwellSatisfied = (nowMs - lastStateChangeMs) >= MIN_STATE_HOLD_MS;

  if (!hvacOn) {
    // Currently OFF, watching for the ON edge.
    if (running) onConfirm++;
    else         onConfirm = 0;
    offConfirm = 0;

    if (onConfirm >= CONFIRM_SAMPLES && dwellSatisfied) {
      hvacOn = true;
      onConfirm = offConfirm = 0;
      lastStateChangeMs = nowMs;
      cycleCount++;
      Serial.printf("[HVAC] %s ON  @ %.2fF  slope=%+.3fF/min  cycle #%lu\n",
                    (SEASON_MODE == 0) ? "HEAT" : "AC  ",
                    tempF, slope, cycleCount);
      publishHvacState(true);
    }
  } else {
    // Currently ON, watching for the OFF edge.
    if (idle) offConfirm++;
    else      offConfirm = 0;
    onConfirm = 0;

    if (offConfirm >= CONFIRM_SAMPLES && dwellSatisfied) {
      hvacOn = false;
      onConfirm = offConfirm = 0;
      lastStateChangeMs = nowMs;
      Serial.printf("[HVAC] %s OFF @ %.2fF  slope=%+.3fF/min\n",
                    (SEASON_MODE == 0) ? "HEAT" : "AC  ",
                    tempF, slope);
      publishHvacState(false);
    }
  }
}

/*
 * Publish the state label.
 *
 * The buffer is deliberately NOT const. AdafruitIO_Feed exposes
 * save(char *value, ...) but no save(const char *, ...). Handing it a string
 * literal makes the compiler reject the char* candidate on const-qualification
 * and silently select save(bool) instead — a pointer-to-bool conversion that
 * always yields true. That is exactly how this feed came to log 1 for every
 * ON and every OFF for two days. Copying into a mutable buffer forces the
 * intended overload. Do not "simplify" this back to a literal.
 */
void publishHvacState(bool on) {
  char buf[10];
  if (SEASON_MODE == 0) strcpy(buf, on ? "HEAT-ON" : "HEAT-OFF");
  else                  strcpy(buf, on ? "AC-ON"   : "AC-OFF");

  if (!feedHvac->save(buf)) {
    Serial.println("[IO]   ERROR: hvac state publish failed.");
  } else {
    Serial.printf("[IO]   hvac state -> %s\n", buf);
  }
  lastStatePubMs = millis();
}

/*
 * Change-triggered publish for humidity/pressure.
 *
 * Fires when |value - lastPub| >= threshold, or when the channel hasn't
 * published in STALE_PUBLISH_MS regardless of movement (heartbeat, mirrors
 * the hvac-state heartbeat above). `lastPub` and `lastPubMs` are updated
 * in place on success.
 */
bool publishIfChanged(AdafruitIO_Feed *feed, const char *label, float value,
                       float &lastPub, uint32_t &lastPubMs, float threshold,
                       uint32_t nowMs) {
  bool firstReading  = isnan(lastPub);
  bool changedEnough = firstReading || (fabs(value - lastPub) >= threshold);
  bool heartbeatDue  = (nowMs - lastPubMs) >= STALE_PUBLISH_MS;

  if (!changedEnough && !heartbeatDue) return false;

  if (feed->save(value)) {
    Serial.printf("[IO]   %s -> %.2f%s\n", label, value,
                  changedEnough ? "" : " (heartbeat)");
    lastPub   = value;
    lastPubMs = nowMs;
    return true;
  }

  Serial.printf("[IO]   ERROR: %s publish failed.\n", label);
  return false;
}

// ─── Setup ──────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(2000);
  Serial.println();
  Serial.println("[BOOT] SmartHome Monitor Level 2 — HVAC State Inference");
  Serial.printf ("[BOOT] Season mode: %s\n", (SEASON_MODE == 0) ? "WINTER" : "SUMMER");
  Serial.printf ("[BOOT] Enter %+.2f  Exit %+.2f °F/min, confirm %u, dwell %lus\n",
                 (SEASON_MODE == 0) ?  SLOPE_ENTER_F_PER_MIN : -SLOPE_ENTER_F_PER_MIN,
                 (SEASON_MODE == 0) ?  SLOPE_EXIT_F_PER_MIN  : -SLOPE_EXIT_F_PER_MIN,
                 CONFIRM_SAMPLES, MIN_STATE_HOLD_MS / 1000);

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

  // Seed the feed so it reflects a real state from boot rather than whatever
  // the last session happened to leave behind.
  lastStateChangeMs = millis();
  publishHvacState(hvacOn);
}

// ─── Main loop ──────────────────────────────────────────────────────────

void loop() {
  io.run();

  uint32_t now = millis();
  if (now - lastSampleMs < SAMPLE_INTERVAL_MS) return;
  lastSampleMs = now;

  float tempC = bme.readTemperature();
  if (isnan(tempC)) {
    Serial.println("[BME]  WARN: bad read, sample skipped.");
    return;
  }
  float tempF = tempC * 9.0f / 5.0f + 32.0f;

  float humidityRh  = bme.readHumidity();      // %RH, NAN on bad read
  float pressureHpa = bme.readPressure() / 100.0f;   // Pa -> hPa

  // Apply exponential smoothing.
  emaTempF = isnan(emaTempF) ? tempF : (EMA_ALPHA * tempF + (1.0f - EMA_ALPHA) * emaTempF);

  if (!isnan(humidityRh)) {
    emaHumidityRh = isnan(emaHumidityRh)
                      ? humidityRh
                      : (EMA_ALPHA * humidityRh + (1.0f - EMA_ALPHA) * emaHumidityRh);
  }
  if (!isnan(pressureHpa)) {
    emaPressureHpa = isnan(emaPressureHpa)
                       ? pressureHpa
                       : (EMA_ALPHA * pressureHpa + (1.0f - EMA_ALPHA) * emaPressureHpa);
  }

  pushSample(now, emaTempF);
  float slope = computeSlopeFPerMin(now);
  updateHvacState(now, emaTempF, slope);

  // Periodic publish of temperature.
  if (now - lastPublishMs >= PUBLISH_INTERVAL_MS) {
    lastPublishMs = now;
    if (feedTemp->save(emaTempF)) {
      Serial.printf("[LOOP] T=%.2fF  slope=%+.3fF/min  hvac=%s  confirm=%u\n",
                    emaTempF,
                    isnan(slope) ? 0.0f : slope,
                    hvacOn ? "ON" : "OFF",
                    hvacOn ? offConfirm : onConfirm);
    }
  }

  // Change-triggered publish of humidity and pressure.
  if (!isnan(emaHumidityRh)) {
    publishIfChanged(feedHumidity, "humidity", emaHumidityRh,
                      lastPubHumidityRh, lastHumidityPubMs,
                      HUMIDITY_CHANGE_THRESHOLD_RH, now);
  }
  if (!isnan(emaPressureHpa)) {
    publishIfChanged(feedPressure, "pressure", emaPressureHpa,
                      lastPubPressureHpa, lastPressurePubMs,
                      PRESSURE_CHANGE_THRESHOLD_HPA, now);
  }

  // Heartbeat: keep the state feed fresh even with no transitions, so a
  // missed edge shows up as a stale-but-correct value rather than silence.
  if (now - lastStatePubMs >= STATE_HEARTBEAT_MS) {
    publishHvacState(hvacOn);
  }
}
