/*
 * SmartHome Monitor — Level 2b: Context-Aware HVAC Inference
 *
 * Extends Level 2 (slope-regression HVAC state detection) with:
 *   - TSL2591 ambient light, split into LED/artificial vs. sunlight using the
 *     IR/visible channel ratio.
 *   - Outdoor temperature (and outdoor shortwave irradiance) from Open-Meteo,
 *     polled every 15 min, no API key.
 *   - A second, optional detector that runs on the slope RESIDUAL after
 *     subtracting modelled passive drift (outdoor coupling + solar gain).
 *
 * Feeds (Adafruit IO group "home"):
 *   existing : home-temperature, home-humidity, home-pressure, home-hvac-state
 *   new      : home-outdoor-temp   °F, every successful poll (15 min)
 *              home-led-lux        lux of artificial light (log-change publish)
 *              home-sun-lux        lux of sunlight         (log-change publish)
 *              home-heat-on        0/1, edges + heartbeat  (analysis)
 *              home-heat-on-base   0/1, baseline detector, only when
 *                                  COMP_ENABLED (A/B comparison)
 *
 * ─────────────────────────────────────────────────────────────────────────
 * ROLLOUT
 *   Phase 1 (default): COMP_ENABLED=false, K_* = 0. Baseline detector only.
 *     Collect lux, outdoor temp, indoor temp for 1–2 weeks.
 *   Phase 2: fit K_COUPLE / K_SOLAR / K_BIAS offline on heat-OFF segments:
 *       dT/dt = K_COUPLE*(T_out - T) + K_SOLAR*log10(1+sun_lux) + K_BIAS
 *   Phase 3: paste the coefficients, set COMP_ENABLED=true, re-derive the
 *     residual thresholds (DET_COMP_*). Both detectors then run side by side.
 *
 * CALIBRATION REQUIRED before trusting led/sun split:
 *   R_SUN and R_LED below are PLACEHOLDERS. Set LIGHT_CALIBRATION=1, then log
 *   the ir/full ratio (Serial) (a) at midday with lamps off -> R_SUN,
 *   (b) at night with only your LEDs on -> R_LED. Re-do inside the final
 *   enclosure: any lid/diffuser changes the spectral response.
 *
 * SEASON_MODE: 0 = WINTER (furnace, temp rises), 1 = SUMMER (AC, temp falls).
 * The slope thresholds were calibrated on SUMMER AC data. Furnace heating
 * rates have NOT been measured yet; treat the thresholds as a starting point
 * and re-derive from the first week of winter data.
 *
 * Libraries: Adafruit BME280, Adafruit TSL2591, Adafruit Unified Sensor,
 *            Adafruit IO Arduino, ArduinoJson (v7). Board: XIAO_ESP32C3.
 * ─────────────────────────────────────────────────────────────────────────
 */

#include <Wire.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>
#include <Adafruit_TSL2591.h>
#include "AdafruitIO_WiFi.h"
#include "secrets.h"

// ─── Feed keys / location (override in secrets.h if you prefer) ─────────
#ifndef IO_FEED_OUTDOOR
#define IO_FEED_OUTDOOR       "home.home-outdoor-temp"
#endif
#ifndef IO_FEED_LED_LUX
#define IO_FEED_LED_LUX       "home.home-led-lux"
#endif
#ifndef IO_FEED_SUN_LUX
#define IO_FEED_SUN_LUX       "home.home-sun-lux"
#endif
#ifndef IO_FEED_HEAT_ON
#define IO_FEED_HEAT_ON       "home.home-heat-on"
#endif
#ifndef IO_FEED_HEAT_ON_BASE
#define IO_FEED_HEAT_ON_BASE  "home.home-heat-on-base"
#endif
#ifndef OUTDOOR_LAT
#define OUTDOOR_LAT           "42.35"     // 2 decimals ≈ 1 km; plenty for a model grid
#endif
#ifndef OUTDOOR_LON
#define OUTDOOR_LON           "-71.11"
#endif

// ─── Season / mode switches ─────────────────────────────────────────────
constexpr uint8_t SEASON_MODE      = 0;       // 0 = WINTER (furnace), 1 = SUMMER (AC)
constexpr bool    COMP_ENABLED     = false;   // run the residual (compensated) detector
#define LIGHT_CALIBRATION 0                   // 1: print raw TSL2591 data every sample

// ─── Passive-drift model coefficients (fit offline; zero = no compensation)
constexpr float K_COUPLE = 0.0f;   // 1/min   : (T_out - T_in) coupling (= 1/tau)
constexpr float K_SOLAR  = 0.0f;   // °F/min per log10(1 + sun_lux)
constexpr float K_BIAS   = 0.0f;   // °F/min  : internal gains offset

// ─── Light source separation (PLACEHOLDERS — calibrate, see header) ─────
constexpr float R_SUN = 0.35f;     // ir/full ratio under pure sunlight
constexpr float R_LED = 0.05f;     // ir/full ratio under pure LED light
constexpr float SUN_MIN_WM2 = 10.0f;   // outdoor shortwave below this => no sun

// ─── Timing ─────────────────────────────────────────────────────────────
constexpr uint8_t  BME280_I2C_ADDR     = 0x77;
constexpr uint32_t SAMPLE_INTERVAL_MS  = 5000;
constexpr uint32_t PUBLISH_INTERVAL_MS = 30000;
constexpr uint32_t STATE_HEARTBEAT_MS  = 300000;    // 5 min
constexpr uint32_t STALE_PUBLISH_MS    = 3600000;   // 1 h (humidity / pressure)
constexpr uint32_t OUTDOOR_POLL_MS     = 900000;    // 15 min (model update interval)
constexpr uint32_t OUTDOOR_VALID_MS    = 5400000;   // 90 min before outdoor data is dropped

// ─── HVAC slope detectors (°F/min magnitudes, signed by season) ─────────
constexpr float DET_BASE_ENTER = 0.25f, DET_BASE_EXIT = 0.08f;   // raw slope
constexpr float DET_COMP_ENTER = 0.25f, DET_COMP_EXIT = 0.08f;   // residual slope (re-derive!)
constexpr uint8_t  CONFIRM_SAMPLES   = 3;
constexpr uint32_t MIN_STATE_HOLD_MS = 90000;

// ─── Change-triggered publish thresholds ────────────────────────────────
constexpr float HUMIDITY_CHANGE_THRESHOLD_RH  = 2.0f;
constexpr float PRESSURE_CHANGE_THRESHOLD_HPA = 2.0f;

// Lux channels: compare in log10 domain. 0.05 decade ≈ 12 %.
constexpr float    LUX_LOG_THRESHOLD = 0.05f;
constexpr float    LUX_FLOOR         = 0.01f;       // lux; below this reads as dark (0)
constexpr uint32_t LUX_MIN_GAP_MS    = 30000;       // rate-limit guard for IO
constexpr uint32_t LUX_HEARTBEAT_MS  = 300000;
constexpr float    LIGHT_EMA_ALPHA   = 0.4f;        // log-domain EMA, reacts fast to switching

// ─── Temperature history for slope regression ───────────────────────────
constexpr uint8_t  HIST_LEN         = 16;
constexpr uint32_t SLOPE_WINDOW_MS  = 60000;
constexpr uint8_t  MIN_SLOPE_POINTS = 6;
constexpr float    EMA_ALPHA        = 0.25f;

// ─── TSL2591 auto-gain ──────────────────────────────────────────────────
// ADC clips lower at 100 ms integration than at >=200 ms; 30000 is safely below.
constexpr uint16_t SAT_COUNTS = 30000;
constexpr uint16_t LOW_COUNTS = 100;
const tsl2591Gain_t GAINS[] = {TSL2591_GAIN_LOW, TSL2591_GAIN_MED,
                               TSL2591_GAIN_HIGH, TSL2591_GAIN_MAX};   // 1x/25x/428x/9876x

// ─── Hardware / IO objects ──────────────────────────────────────────────
Adafruit_BME280 bme;
Adafruit_TSL2591 tsl = Adafruit_TSL2591(2591);
AdafruitIO_WiFi io(IO_USERNAME, IO_KEY, WIFI_SSID, WIFI_PASS);
AdafruitIO_Feed *feedTemp     = io.feed(IO_FEED_TEMP);
AdafruitIO_Feed *feedHvac     = io.feed(IO_FEED_HVAC);
AdafruitIO_Feed *feedHumidity = io.feed(IO_FEED_HUMID);
AdafruitIO_Feed *feedPressure = io.feed(IO_FEED_PRESS);
AdafruitIO_Feed *feedOutdoor  = io.feed(IO_FEED_OUTDOOR);
AdafruitIO_Feed *feedLedLux   = io.feed(IO_FEED_LED_LUX);
AdafruitIO_Feed *feedSunLux   = io.feed(IO_FEED_SUN_LUX);
AdafruitIO_Feed *feedHeatOn   = io.feed(IO_FEED_HEAT_ON);
AdafruitIO_Feed *feedHeatBase = io.feed(IO_FEED_HEAT_ON_BASE);

// ─── Detector (Schmitt trigger + confirmation + dwell) ──────────────────
struct Detector {
  const char *name;
  float enter, leave;          // magnitudes
  bool on = false;
  uint8_t onC = 0, offC = 0;
  uint32_t lastChangeMs = 0;
  uint32_t cycles = 0;

  // Explicit ctor: a class with default member initializers is not an
  // aggregate under C++11, which older ESP32 Arduino cores still compile with.
  Detector(const char *n, float e, float l) : name(n), enter(e), leave(l) {}

  // Returns true when the state changed on this call.
  bool update(uint32_t now, float tempF, float slope) {
    if (isnan(slope)) return false;
    // Season-agnostic: s > 0 means "moving the way the equipment drives it".
    float s = (SEASON_MODE == 0) ? slope : -slope;
    bool running = (s >  enter);
    bool idle    = (s <  leave);
    bool dwellOk = (now - lastChangeMs) >= MIN_STATE_HOLD_MS;

    if (!on) {
      onC = running ? onC + 1 : 0;
      offC = 0;
      if (onC >= CONFIRM_SAMPLES && dwellOk) {
        on = true; onC = offC = 0; lastChangeMs = now; cycles++;
        Serial.printf("[%s] ON  @ %.2fF  slope=%+.3f  cycle #%lu\n", name, tempF, slope, cycles);
        return true;
      }
    } else {
      offC = idle ? offC + 1 : 0;
      onC = 0;
      if (offC >= CONFIRM_SAMPLES && dwellOk) {
        on = false; onC = offC = 0; lastChangeMs = now;
        Serial.printf("[%s] OFF @ %.2fF  slope=%+.3f\n", name, tempF, slope);
        return true;
      }
    }
    return false;
  }
};

Detector detBase = {"BASE", DET_BASE_ENTER, DET_BASE_EXIT};
Detector detComp = {"COMP", DET_COMP_ENTER, DET_COMP_EXIT};

// ─── State ──────────────────────────────────────────────────────────────
struct TempSample { uint32_t ms; float tempF; };
TempSample history[HIST_LEN] = {};
uint8_t histCount = 0, histHead = 0;

float emaTempF = NAN, emaHumidityRh = NAN, emaPressureHpa = NAN;
float lastPubHumidityRh = NAN, lastPubPressureHpa = NAN;

struct Light { float lux = NAN, irFrac = NAN; uint16_t full = 0, ir = 0; };
bool  lightOk = false;
uint8_t gainIdx = 1;
Light lastLight;
float emaLogSun = NAN, emaLogLed = NAN;       // log10(lux), floored
float lastPubSun = NAN, lastPubLed = NAN;

struct Outdoor { float tempF = NAN, swWm2 = NAN; uint32_t okMs = 0; };
Outdoor outdoor;
uint32_t nextOutdoorMs = 0;
uint8_t  outdoorFails  = 0;

uint32_t lastSampleMs = 0, lastPublishMs = 0, lastStatePubMs = 0;
uint32_t lastHumidityPubMs = 0, lastPressurePubMs = 0;
uint32_t lastSunPubMs = 0, lastLedPubMs = 0;

// ─── History / slope ────────────────────────────────────────────────────
void pushSample(uint32_t nowMs, float tempF) {
  history[histHead] = {nowMs, tempF};
  histHead = (histHead + 1) % HIST_LEN;
  if (histCount < HIST_LEN) histCount++;
}

float computeSlopeFPerMin(uint32_t nowMs) {
  if (histCount < MIN_SLOPE_POINTS) return NAN;
  float sumT = 0, sumY = 0, sumTT = 0, sumTY = 0;
  uint8_t n = 0;
  for (uint8_t i = 0; i < histCount; i++) {
    uint8_t idx = (histHead + HIST_LEN - 1 - i) % HIST_LEN;
    uint32_t age = nowMs - history[idx].ms;
    if (age > SLOPE_WINDOW_MS) continue;
    float t = -(float)age / 60000.0f;
    float y = history[idx].tempF;
    sumT += t; sumY += y; sumTT += t * t; sumTY += t * y;
    n++;
  }
  if (n < MIN_SLOPE_POINTS) return NAN;
  float denom = (float)n * sumTT - sumT * sumT;
  if (fabsf(denom) < 1e-6f) return NAN;
  return ((float)n * sumTY - sumT * sumY) / denom;
}

// ─── Outdoor weather (Open-Meteo) ───────────────────────────────────────
bool outdoorFresh(uint32_t now) {
  return !isnan(outdoor.tempF) && (now - outdoor.okMs) <= OUTDOOR_VALID_MS;
}

bool fetchOutdoor() {
  if (WiFi.status() != WL_CONNECTED) return false;

  WiFiClientSecure client;
  client.setInsecure();      // bring-up. Public weather data only; pin the CA later if desired.
  HTTPClient http;
  http.setConnectTimeout(4000);
  http.setTimeout(4000);

  String url = String("https://api.open-meteo.com/v1/forecast?latitude=") + OUTDOOR_LAT +
               "&longitude=" + OUTDOOR_LON +
               "&current=temperature_2m,shortwave_radiation&temperature_unit=fahrenheit";
  if (!http.begin(client, url)) return false;
  int code = http.GET();
  if (code != 200) {
    Serial.printf("[WX]   HTTP %d\n", code);
    http.end();
    return false;
  }
  String body = http.getString();     // not getStream(): response may be chunked
  http.end();

  JsonDocument doc;
  if (deserializeJson(doc, body)) { Serial.println("[WX]   JSON parse failed."); return false; }
  JsonVariantConst cur = doc["current"];
  float t  = cur["temperature_2m"]       | NAN;
  float sw = cur["shortwave_radiation"]  | NAN;
  if (isnan(t) || t < -60.0f || t > 130.0f) return false;     // sanity range, °F

  outdoor.tempF = t;
  outdoor.swWm2 = sw;
  outdoor.okMs  = millis();
  Serial.printf("[WX]   outdoor %.1fF  shortwave %.0f W/m2\n", t, isnan(sw) ? -1.0f : sw);
  return true;
}

void serviceOutdoor(uint32_t now) {
  if ((int32_t)(now - nextOutdoorMs) < 0) return;
  if (fetchOutdoor()) {
    outdoorFails = 0;
    nextOutdoorMs = now + OUTDOOR_POLL_MS;
    if (feedOutdoor->save(outdoor.tempF)) Serial.printf("[IO]   outdoor -> %.1f\n", outdoor.tempF);
  } else {
    if (outdoorFails < 250) outdoorFails++;
    uint32_t backoff = 60000UL << (outdoorFails > 5 ? 4 : outdoorFails - 1);   // 1,2,4,8,16 min
    nextOutdoorMs = now + min(backoff, OUTDOOR_POLL_MS);
    Serial.printf("[WX]   fetch failed (%u), retry in %lus\n", outdoorFails,
                  min(backoff, OUTDOOR_POLL_MS) / 1000);
  }
}

// ─── Light (TSL2591) ────────────────────────────────────────────────────
// Returns false if the reading was discarded (gain still settling / saturated).
bool readLight(Light &out) {
  for (uint8_t attempt = 0; attempt < 4; attempt++) {
    uint32_t lum = tsl.getFullLuminosity();       // blocks ~integration time (100 ms)
    uint16_t ir = lum >> 16, full = lum & 0xFFFF;

    if (full >= SAT_COUNTS) {
      if (gainIdx == 0) return false;             // saturated at minimum gain
      tsl.setGain(GAINS[--gainIdx]);
      continue;
    }
    if (full < LOW_COUNTS && gainIdx < 3) {
      tsl.setGain(GAINS[++gainIdx]);
      continue;
    }

    // calculateLux() divides by ch0: guard the full==0 (dark) case, and clamp
    // NaN / negative results caused by ir > full noise.
    float lux = (full == 0) ? 0.0f : tsl.calculateLux(full, ir);
    if (!(lux > 0.0f)) lux = 0.0f;

    out.full = full;  out.ir = ir;  out.lux = lux;
    out.irFrac = (full > 200) ? (float)ir / (float)full : NAN;   // noise-floor guard
    return true;
  }
  return false;
}

// Split total lux into sun / LED and update the log-domain EMAs.
void updateLight(const Light &lt, uint32_t now) {
  // Outdoor shortwave gates the sun term when we have fresh data (night => no sun).
  bool sunAllowed = true;
  if (outdoorFresh(now) && !isnan(outdoor.swWm2)) sunAllowed = (outdoor.swWm2 > SUN_MIN_WM2);

  // ir = R_SUN*S + R_LED*(full - S)  =>  S/full = (ratio - R_LED) / (R_SUN - R_LED)
  float sunFrac = 0.0f;
  if (sunAllowed && !isnan(lt.irFrac)) {
    sunFrac = (lt.irFrac - R_LED) / (R_SUN - R_LED);
    sunFrac = constrain(sunFrac, 0.0f, 1.0f);
  }
  float sunLux = lt.lux * sunFrac;
  float ledLux = lt.lux - sunLux;

  float lSun = log10f(fmaxf(sunLux, LUX_FLOOR));
  float lLed = log10f(fmaxf(ledLux, LUX_FLOOR));
  emaLogSun = isnan(emaLogSun) ? lSun : LIGHT_EMA_ALPHA * lSun + (1.0f - LIGHT_EMA_ALPHA) * emaLogSun;
  emaLogLed = isnan(emaLogLed) ? lLed : LIGHT_EMA_ALPHA * lLed + (1.0f - LIGHT_EMA_ALPHA) * emaLogLed;
  lastLight = lt;
}

float logToLux(float logv) {
  const float lf = log10f(LUX_FLOOR);
  return (logv <= lf + 0.02f) ? 0.0f : powf(10.0f, logv);
}

bool publishLuxIfChanged(AdafruitIO_Feed *feed, const char *label, float emaLog,
                         float &lastPub, uint32_t &lastPubMs, uint32_t now) {
  if (isnan(emaLog)) return false;
  float value = logToLux(emaLog);
  bool first = isnan(lastPub);
  float lastLog = first ? 0.0f : log10f(fmaxf(lastPub, LUX_FLOOR));
  bool changed   = first || fabsf(emaLog - lastLog) >= LUX_LOG_THRESHOLD;
  bool gapOk     = (now - lastPubMs) >= LUX_MIN_GAP_MS;
  bool heartbeat = (now - lastPubMs) >= LUX_HEARTBEAT_MS;
  if (!(first || (changed && gapOk) || heartbeat)) return false;

  if (feed->save(value)) {
    Serial.printf("[IO]   %s -> %.2f lux%s\n", label, value, changed ? "" : " (heartbeat)");
    lastPub = value;  lastPubMs = now;
    return true;
  }
  Serial.printf("[IO]   ERROR: %s publish failed.\n", label);
  return false;
}

// ─── Passive-drift model ────────────────────────────────────────────────
// Expected °F/min of drift with the equipment OFF. Zero when compensation is
// unavailable, so the compensated detector degrades to the baseline.
float passiveDriftFPerMin(float tIn, uint32_t now) {
  if (!outdoorFresh(now)) return 0.0f;
  float sunTerm = isnan(emaLogSun) ? 0.0f : log10f(1.0f + logToLux(emaLogSun));
  return K_COUPLE * (outdoor.tempF - tIn) + K_SOLAR * sunTerm + K_BIAS;
}

// ─── Publishing ─────────────────────────────────────────────────────────
// Mutable buffer on purpose: AdafruitIO_Feed::save(const char*) does not exist,
// and a literal silently binds to save(bool). See docs/HVAC_STATE_BUG.md.
void publishHeatState(bool on) {
  char buf[10];
  if (SEASON_MODE == 0) strcpy(buf, on ? "HEAT-ON" : "HEAT-OFF");
  else                  strcpy(buf, on ? "AC-ON"   : "AC-OFF");

  bool ok = feedHvac->save(buf);
  ok &= feedHeatOn->save((int)(on ? 1 : 0));
  if (COMP_ENABLED) ok &= feedHeatBase->save((int)(detBase.on ? 1 : 0));
  Serial.println(ok ? "[IO]   heat state published." : "[IO]   ERROR: heat state publish failed.");
  lastStatePubMs = millis();
}

bool publishIfChanged(AdafruitIO_Feed *feed, const char *label, float value,
                      float &lastPub, uint32_t &lastPubMs, float threshold, uint32_t now) {
  bool first = isnan(lastPub);
  bool changed = first || (fabsf(value - lastPub) >= threshold);
  bool heartbeat = (now - lastPubMs) >= STALE_PUBLISH_MS;
  if (!changed && !heartbeat) return false;
  if (feed->save(value)) {
    Serial.printf("[IO]   %s -> %.2f%s\n", label, value, changed ? "" : " (heartbeat)");
    lastPub = value;  lastPubMs = now;
    return true;
  }
  Serial.printf("[IO]   ERROR: %s publish failed.\n", label);
  return false;
}

const Detector &primary() { return COMP_ENABLED ? detComp : detBase; }

// ─── Setup ──────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(2000);
  Serial.println();
  Serial.println("[BOOT] SmartHome Monitor Level 2b — context-aware HVAC inference");
  Serial.printf ("[BOOT] Season: %s   compensation: %s\n",
                 (SEASON_MODE == 0) ? "WINTER" : "SUMMER", COMP_ENABLED ? "ON" : "off");

  Wire.begin();
  if (!bme.begin(BME280_I2C_ADDR, &Wire) && !bme.begin(0x76, &Wire)) {
    Serial.println("[BME]  ERROR: sensor not found. Halting.");
    while (1) delay(1000);
  }
  bme.setSampling(Adafruit_BME280::MODE_NORMAL,
                  Adafruit_BME280::SAMPLING_X2, Adafruit_BME280::SAMPLING_X16,
                  Adafruit_BME280::SAMPLING_X1, Adafruit_BME280::FILTER_X16,
                  Adafruit_BME280::STANDBY_MS_500);
  Serial.println("[BME]  Sensor initialized.");

  // The light channel is optional: the HVAC detector must survive without it.
  lightOk = tsl.begin();
  if (lightOk) {
    tsl.setTiming(TSL2591_INTEGRATIONTIME_100MS);
    tsl.setGain(GAINS[gainIdx]);
    Serial.println("[TSL]  Sensor initialized (0x29).");
  } else {
    Serial.println("[TSL]  WARN: TSL2591 not found; light feeds disabled.");
  }

  Serial.print("[WIFI] Connecting");
  io.connect();
  while (io.status() < AIO_CONNECTED) { Serial.print("."); delay(500); }
  Serial.printf("\n[IO]   %s\n", io.statusText());

  uint32_t now = millis();
  detBase.lastChangeMs = detComp.lastChangeMs = now;
  nextOutdoorMs = now;                // fetch on the first loop pass
  publishHeatState(primary().on);
}

// ─── Main loop ──────────────────────────────────────────────────────────
void loop() {
  io.run();

  uint32_t now = millis();
  if (now - lastSampleMs < SAMPLE_INTERVAL_MS) return;
  lastSampleMs = now;

  // --- BME280 ---
  float tempC = bme.readTemperature();
  if (isnan(tempC)) { Serial.println("[BME]  WARN: bad read, sample skipped."); return; }
  float tempF = tempC * 9.0f / 5.0f + 32.0f;
  float humidityRh  = bme.readHumidity();
  float pressureHpa = bme.readPressure() / 100.0f;

  emaTempF = isnan(emaTempF) ? tempF : (EMA_ALPHA * tempF + (1.0f - EMA_ALPHA) * emaTempF);
  if (!isnan(humidityRh))
    emaHumidityRh = isnan(emaHumidityRh) ? humidityRh
                  : (EMA_ALPHA * humidityRh + (1.0f - EMA_ALPHA) * emaHumidityRh);
  if (!isnan(pressureHpa))
    emaPressureHpa = isnan(emaPressureHpa) ? pressureHpa
                   : (EMA_ALPHA * pressureHpa + (1.0f - EMA_ALPHA) * emaPressureHpa);

  // --- TSL2591 ---
  if (lightOk) {
    Light lt;
    if (readLight(lt)) {
      updateLight(lt, now);
#if LIGHT_CALIBRATION
      Serial.printf("[CAL]  full=%u ir=%u ratio=%.3f gain#%u lux=%.2f\n",
                    lt.full, lt.ir, isnan(lt.irFrac) ? -1.0f : lt.irFrac, gainIdx, lt.lux);
#endif
    }
  }

  // --- HVAC inference ---
  pushSample(now, emaTempF);
  float slope    = computeSlopeFPerMin(now);
  float residual = isnan(slope) ? NAN : slope - passiveDriftFPerMin(emaTempF, now);

  bool edge = detBase.update(now, emaTempF, slope);
  if (COMP_ENABLED) edge = detComp.update(now, emaTempF, residual) || edge;
  if (edge) publishHeatState(primary().on);

  // --- Publishing ---
  if (now - lastPublishMs >= PUBLISH_INTERVAL_MS) {
    lastPublishMs = now;
    if (feedTemp->save(emaTempF)) {
      Serial.printf("[LOOP] T=%.2fF slope=%+.3f resid=%+.3f heat=%s | out=%.1fF sun=%.1f led=%.1f lux\n",
                    emaTempF, isnan(slope) ? 0.0f : slope, isnan(residual) ? 0.0f : residual,
                    primary().on ? "ON" : "OFF",
                    outdoorFresh(now) ? outdoor.tempF : NAN,
                    logToLux(isnan(emaLogSun) ? -9.0f : emaLogSun),
                    logToLux(isnan(emaLogLed) ? -9.0f : emaLogLed));
    }
  }

  if (!isnan(emaHumidityRh))
    publishIfChanged(feedHumidity, "humidity", emaHumidityRh, lastPubHumidityRh,
                     lastHumidityPubMs, HUMIDITY_CHANGE_THRESHOLD_RH, now);
  if (!isnan(emaPressureHpa))
    publishIfChanged(feedPressure, "pressure", emaPressureHpa, lastPubPressureHpa,
                     lastPressurePubMs, PRESSURE_CHANGE_THRESHOLD_HPA, now);

#if !LIGHT_CALIBRATION
  if (lightOk) {
    publishLuxIfChanged(feedSunLux, "sun", emaLogSun, lastPubSun, lastSunPubMs, now);
    publishLuxIfChanged(feedLedLux, "led", emaLogLed, lastPubLed, lastLedPubMs, now);
  }
#endif

  if (now - lastStatePubMs >= STATE_HEARTBEAT_MS) publishHeatState(primary().on);

  // Outdoor fetch last: it can block 1–2 s on TLS, and timestamps are millis()-based
  // so the slope regression is unaffected by a late next sample.
  serviceOutdoor(now);
}
