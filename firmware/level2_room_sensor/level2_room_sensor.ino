/*
 * SmartHome Monitor — Level 2: Room Sensor (main + peripheral)
 *
 * One sketch for every board in the house. DEVICE_ROLE in secrets.h selects
 * what this board does:
 *
 *   ROLE_MAIN        living room. BME280 temperature + humidity, TSL2591 light
 *                    split into lamp/LED and sunlight, and outdoor temperature
 *                    from Open-Meteo (no API key).
 *   ROLE_PERIPHERAL  bedroom. BME280 temperature + humidity.
 *
 * Each board connects to Adafruit IO on its own, and both share the account's
 * free-tier budget (10 feeds, 30 messages/min). Values worth charting get a
 * numeric feed. Diagnostics that only matter offline (uptime, WiFi RSSI, free
 * heap, raw TSL2591 counts) are packed into one text feed per board as
 * "key=value" pairs, so they cost one feed instead of seven; the analysis in
 * collected-data/ parses them back out of the CSV export. See README.md for
 * the feed list and the message budget.
 *
 * Libraries: Adafruit BME280, Adafruit TSL2591, Adafruit Unified Sensor,
 *            Adafruit IO Arduino, ArduinoJson (v7). Board: XIAO_ESP32C3.
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

#define ROLE_MAIN       1
#define ROLE_PERIPHERAL 2
#include "secrets.h"

// ─── Role: room name and feed keys (Adafruit IO group "home") ───────────
#if DEVICE_ROLE == ROLE_MAIN
  #define ROOM        "living"
  // Same keys as Level 1, so the living-room history continues unbroken.
  #define FEED_TEMP   "home.home-temperature"
  #define FEED_HUMID  "home.home-humidity"
  #define FEED_STATUS "home.home-living-status"
  #if !defined(OUTDOOR_LAT) || !defined(OUTDOOR_LON)
    #error "The main board needs OUTDOOR_LAT and OUTDOOR_LON in secrets.h"
  #endif
#elif DEVICE_ROLE == ROLE_PERIPHERAL
  #define ROOM        "bedroom"
  #define FEED_TEMP   "home.home-bedroom-temperature"
  #define FEED_HUMID  "home.home-bedroom-humidity"
  #define FEED_STATUS "home.home-bedroom-status"
  #ifndef OUTDOOR_LAT
    #define OUTDOOR_LAT ""            // unused: the peripheral never fetches weather
    #define OUTDOOR_LON ""
  #endif
#else
  #error "Set DEVICE_ROLE in secrets.h to ROLE_MAIN or ROLE_PERIPHERAL"
#endif
#define FEED_OUTDOOR  "home.home-outdoor-temp"   // main only
#define FEED_LED_LUX  "home.home-led-lux"        // main only
#define FEED_SUN_LUX  "home.home-sun-lux"        // main only

constexpr bool IS_MAIN = (DEVICE_ROLE == ROLE_MAIN);

// ─── Timing ─────────────────────────────────────────────────────────────
constexpr uint32_t SAMPLE_INTERVAL_MS    = 5000;      // sensor read + filter update
constexpr uint32_t TEMP_PUBLISH_MS       = 30000;
constexpr uint32_t STATUS_PUBLISH_MS     = 300000;    // 5 min
constexpr uint32_t OUTDOOR_POLL_MS       = 900000;    // 15 min
constexpr uint32_t OUTDOOR_VALID_MS      = 5400000;   // outdoor data goes stale after 90 min
constexpr uint32_t IO_CONNECT_TIMEOUT_MS = 60000;

// ─── Filtering and publish-on-change ────────────────────────────────────
constexpr float    EMA_ALPHA          = 0.25f;     // temperature + humidity, per 5 s sample
constexpr float    HUMID_CHANGE_RH    = 1.0f;
constexpr uint32_t HUMID_MIN_GAP_MS   = 30000;
constexpr uint32_t HUMID_HEARTBEAT_MS = 1800000;   // 30 min
constexpr float    LIGHT_EMA_ALPHA    = 0.4f;      // log domain; follows lamps switching quickly
constexpr float    LUX_CHANGE_DECADES = 0.05f;     // ≈ 12 %
constexpr uint32_t LUX_MIN_GAP_MS     = 30000;
constexpr uint32_t LUX_HEARTBEAT_MS   = 300000;    // 5 min
// Lux is never published below this. Adafruit IO's log-scale charts cannot
// draw 0, so a dark room is sent as 0.01 lux instead.
constexpr float    LUX_FLOOR          = 0.01f;

// ─── Light source split (PLACEHOLDERS: calibrate, see README) ───────────
constexpr float R_SUN       = 0.35f;   // ir/full ratio under pure sunlight
constexpr float R_LED       = 0.05f;   // ir/full ratio under pure LED light
constexpr float SUN_MIN_WM2 = 10.0f;   // outdoor shortwave below this => no sun

// ─── TSL2591 auto-gain ──────────────────────────────────────────────────
constexpr uint16_t SAT_COUNTS = 30000;   // safely below the clip point at 100 ms integration
constexpr uint16_t LOW_COUNTS = 100;
const tsl2591Gain_t GAINS[]   = {TSL2591_GAIN_LOW, TSL2591_GAIN_MED,
                                 TSL2591_GAIN_HIGH, TSL2591_GAIN_MAX};
const uint16_t GAIN_X[]       = {1, 25, 428, 9876};

// ─── Hardware / IO objects ──────────────────────────────────────────────
Adafruit_BME280  bme;
Adafruit_TSL2591 tsl(2591);
AdafruitIO_WiFi  io(IO_USERNAME, IO_KEY, WIFI_SSID, WIFI_PASS);
AdafruitIO_Feed *feedTemp    = io.feed(FEED_TEMP);
AdafruitIO_Feed *feedStatus  = io.feed(FEED_STATUS);
AdafruitIO_Feed *feedOutdoor = IS_MAIN ? io.feed(FEED_OUTDOOR) : nullptr;

// Publishes when the value has moved by `threshold` (no sooner than minGapMs
// after the last publish), and every heartbeatMs regardless. logScale compares
// in decades, which suits lux spanning 0.01 to 100 000.
struct ChangeFeed {
  AdafruitIO_Feed *feed;
  const char *label;
  float threshold;
  bool logScale;
  uint32_t minGapMs, heartbeatMs;
  float last = NAN;
  uint32_t lastMs = 0;

  ChangeFeed(AdafruitIO_Feed *f, const char *l, float t, bool lg, uint32_t gap, uint32_t hb)
      : feed(f), label(l), threshold(t), logScale(lg), minGapMs(gap), heartbeatMs(hb) {}

  void update(float value, uint32_t now) {
    if (feed == nullptr || isnan(value)) return;
    bool first = isnan(last);
    uint32_t age = now - lastMs;
    float delta = first ? 0.0f : logScale ? fabsf(log10f(value / last)) : fabsf(value - last);
    bool changed = !first && delta >= threshold && age >= minGapMs;
    if (!first && !changed && age < heartbeatMs) return;
    if (feed->save(value)) {
      Serial.printf("[IO]   %s -> %.2f%s\n", label, value, (first || changed) ? "" : " (heartbeat)");
      last = value;
      lastMs = now;
    } else {
      Serial.printf("[IO]   ERROR: %s publish failed.\n", label);
    }
  }
};

ChangeFeed humid (io.feed(FEED_HUMID), "humidity", HUMID_CHANGE_RH, false,
                  HUMID_MIN_GAP_MS, HUMID_HEARTBEAT_MS);
ChangeFeed sunLux(IS_MAIN ? io.feed(FEED_SUN_LUX) : nullptr, "sun lux", LUX_CHANGE_DECADES, true,
                  LUX_MIN_GAP_MS, LUX_HEARTBEAT_MS);
ChangeFeed ledLux(IS_MAIN ? io.feed(FEED_LED_LUX) : nullptr, "led lux", LUX_CHANGE_DECADES, true,
                  LUX_MIN_GAP_MS, LUX_HEARTBEAT_MS);

// ─── State ──────────────────────────────────────────────────────────────
float emaTempF = NAN, emaHumidity = NAN;
uint32_t lastSampleMs = 0, lastTempPubMs = 0, lastStatusMs = 0;

struct Light { uint16_t full = 0, ir = 0, gain = 0; float lux = NAN; };
bool    lightOk = false;
uint8_t gainIdx = 1;
Light   lastLight;
float   emaLogSun = NAN, emaLogLed = NAN;     // log10(lux), floored at LUX_FLOOR

struct Outdoor { float tempF = NAN, swWm2 = NAN; uint32_t okMs = 0; };
Outdoor  outdoor;
uint32_t nextOutdoorMs = 0;
uint8_t  outdoorFails  = 0;

// ─── Helpers ────────────────────────────────────────────────────────────
float ema(float prev, float x, float alpha) {
  return isnan(prev) ? x : alpha * x + (1.0f - alpha) * prev;
}

// True (and re-arms) when `interval` has passed since `last`; always true the first time.
bool due(uint32_t &last, uint32_t interval, uint32_t now) {
  if (last != 0 && now - last < interval) return false;
  last = now;
  return true;
}

float luxOf(float logLux) { return fmaxf(powf(10.0f, logLux), LUX_FLOOR); }   // NAN stays NAN

// ─── BME280 ─────────────────────────────────────────────────────────────
void sampleBme() {
  float tempC = bme.readTemperature();
  float rh    = bme.readHumidity();
  if (isnan(tempC)) { Serial.println("[BME]  WARN: bad read, sample skipped."); return; }
  emaTempF = ema(emaTempF, tempC * 9.0f / 5.0f + 32.0f, EMA_ALPHA);
  if (!isnan(rh)) emaHumidity = ema(emaHumidity, rh, EMA_ALPHA);
}

// ─── Light (TSL2591, main only) ─────────────────────────────────────────
// Returns false if the reading was discarded (gain still settling, or saturated).
bool readLight(Light &out) {
  for (uint8_t attempt = 0; attempt < 4; attempt++) {
    uint32_t lum = tsl.getFullLuminosity();       // blocks for the 100 ms integration
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
    // calculateLux() divides by full: guard the dark case, and clamp the
    // NaN / negative results that ir > full noise produces.
    float lux = (full == 0) ? 0.0f : tsl.calculateLux(full, ir);
    out.full = full;
    out.ir   = ir;
    out.gain = GAIN_X[gainIdx];
    out.lux  = (lux > 0.0f) ? lux : 0.0f;
    return true;
  }
  return false;
}

bool outdoorFresh(uint32_t now) {
  return !isnan(outdoor.tempF) && (now - outdoor.okMs) <= OUTDOOR_VALID_MS;
}

// Split total lux into sun and LED by the IR fraction, and update the log-domain EMAs.
void updateLight(const Light &lt, uint32_t now) {
  // When outdoor shortwave says it is night, every lux is artificial.
  bool haveSw      = outdoorFresh(now) && !isnan(outdoor.swWm2);
  bool sunPossible = !haveSw || outdoor.swWm2 > SUN_MIN_WM2;

  // Two-source mix: ir = R_SUN*S + R_LED*(full - S)  =>  S/full = (ratio - R_LED) / (R_SUN - R_LED).
  // Below 200 counts the ratio is noise, so dim light is all counted as LED.
  float sunFrac = 0.0f;
  if (sunPossible && lt.full > 200) {
    float ratio = (float)lt.ir / (float)lt.full;
    sunFrac = constrain((ratio - R_LED) / (R_SUN - R_LED), 0.0f, 1.0f);
  }
  float sun = lt.lux * sunFrac;
  emaLogSun = ema(emaLogSun, log10f(fmaxf(sun, LUX_FLOOR)), LIGHT_EMA_ALPHA);
  emaLogLed = ema(emaLogLed, log10f(fmaxf(lt.lux - sun, LUX_FLOOR)), LIGHT_EMA_ALPHA);
  lastLight = lt;
}

// ─── Outdoor weather (Open-Meteo, main only) ────────────────────────────
bool fetchOutdoor() {
  if (WiFi.status() != WL_CONNECTED) return false;

  WiFiClientSecure client;
  client.setInsecure();      // public weather data only; pin the CA if that matters to you
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
  String body = http.getString();     // not getStream(): the response may be chunked
  http.end();

  JsonDocument doc;
  if (deserializeJson(doc, body)) { Serial.println("[WX]   JSON parse failed."); return false; }
  JsonVariantConst cur = doc["current"];
  float t  = cur["temperature_2m"]      | NAN;
  float sw = cur["shortwave_radiation"] | NAN;
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
    outdoorFails  = 0;
    nextOutdoorMs = now + OUTDOOR_POLL_MS;
    if (feedOutdoor->save(outdoor.tempF)) Serial.printf("[IO]   outdoor -> %.1f\n", outdoor.tempF);
  } else {
    if (outdoorFails < 250) outdoorFails++;
    uint32_t backoff = 60000UL << (outdoorFails > 5 ? 4 : outdoorFails - 1);   // 1, 2, 4, 8, then 15 min
    if (backoff > OUTDOOR_POLL_MS) backoff = OUTDOOR_POLL_MS;
    nextOutdoorMs = now + backoff;
    Serial.printf("[WX]   fetch failed (%u), retry in %lus\n", outdoorFails, (unsigned long)(backoff / 1000));
  }
}

// ─── Status (packed text feed) ──────────────────────────────────────────
// e.g. "up=86400 rssi=-61 heap=182344 full=1534 ir=212 gain=25 lux=31.200"
// The buffer must be mutable: AdafruitIO_Feed has save(char *) but no
// save(const char *), and a string literal silently binds to save(bool).
void publishStatus(uint32_t now) {
  char buf[128];
  int n = snprintf(buf, sizeof(buf), "up=%lu rssi=%d heap=%lu", (unsigned long)(now / 1000),
                   (int)WiFi.RSSI(), (unsigned long)ESP.getFreeHeap());
  if (IS_MAIN && lightOk && !isnan(lastLight.lux))
    snprintf(buf + n, sizeof(buf) - n, " full=%u ir=%u gain=%u lux=%.3f",
             lastLight.full, lastLight.ir, lastLight.gain, lastLight.lux);
  if (feedStatus->save(buf)) Serial.printf("[IO]   status -> %s\n", buf);
  else                       Serial.println("[IO]   ERROR: status publish failed.");
}

void publishTemperature(uint32_t now) {
  if (!feedTemp->save(emaTempF)) { Serial.println("[IO]   ERROR: temperature publish failed."); return; }
  Serial.printf("[LOOP] %s T=%.2fF H=%.1f%%", ROOM, emaTempF, emaHumidity);
  if (IS_MAIN && lightOk)
    Serial.printf(" | sun=%.2f led=%.2f lux", luxOf(emaLogSun), luxOf(emaLogLed));
  if (IS_MAIN && outdoorFresh(now))
    Serial.printf(" | out=%.1fF", outdoor.tempF);
  Serial.println();
}

// ─── Setup ──────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(2000);                                    // let the serial monitor attach
  Serial.printf("\n[BOOT] SmartHome Monitor Level 2 — room sensor: %s (%s)\n",
                ROOM, IS_MAIN ? "main" : "peripheral");

  Wire.begin();
  if (!bme.begin(0x77, &Wire) && !bme.begin(0x76, &Wire)) {
    Serial.println("[BME]  ERROR: sensor not found at 0x77 or 0x76. Restarting in 10 s.");
    delay(10000);
    ESP.restart();
  }
  // Temperature 2x, pressure off (unused), humidity 1x, IIR filter 16.
  bme.setSampling(Adafruit_BME280::MODE_NORMAL,
                  Adafruit_BME280::SAMPLING_X2, Adafruit_BME280::SAMPLING_NONE,
                  Adafruit_BME280::SAMPLING_X1, Adafruit_BME280::FILTER_X16,
                  Adafruit_BME280::STANDBY_MS_500);
  Serial.println("[BME]  Sensor initialized.");

  if (IS_MAIN) {
    // Light is optional: temperature keeps publishing without it.
    lightOk = tsl.begin();
    if (lightOk) {
      tsl.setTiming(TSL2591_INTEGRATIONTIME_100MS);
      tsl.setGain(GAINS[gainIdx]);
      Serial.println("[TSL]  Sensor initialized.");
    } else {
      Serial.println("[TSL]  WARN: TSL2591 not found; light feeds disabled.");
    }
  }

  Serial.print("[WIFI] Connecting");
  io.connect();
  uint32_t t0 = millis();
  while (io.status() < AIO_CONNECTED) {
    if (millis() - t0 > IO_CONNECT_TIMEOUT_MS) {
      Serial.printf("\n[WIFI] ERROR: %s. Restarting.\n", reinterpret_cast<const char *>(io.statusText()));
      ESP.restart();
    }
    Serial.print(".");
    delay(500);
  }
  Serial.printf("\n[IO]   %s\n", reinterpret_cast<const char *>(io.statusText()));
  nextOutdoorMs = millis();                       // fetch on the first loop pass
}

// ─── Main loop ──────────────────────────────────────────────────────────
void loop() {
  io.run();                                       // MQTT keep-alive; call every iteration

  uint32_t now = millis();
  if (!due(lastSampleMs, SAMPLE_INTERVAL_MS, now)) return;

  sampleBme();
  if (IS_MAIN && lightOk) {
    Light lt;
    if (readLight(lt)) updateLight(lt, now);
  }

  if (!isnan(emaTempF) && due(lastTempPubMs, TEMP_PUBLISH_MS, now)) publishTemperature(now);
  humid.update(emaHumidity, now);
  if (IS_MAIN && lightOk) {
    sunLux.update(luxOf(emaLogSun), now);
    ledLux.update(luxOf(emaLogLed), now);
  }
  if (due(lastStatusMs, STATUS_PUBLISH_MS, now)) publishStatus(now);

  // Last: the HTTPS fetch can block for 1–2 s.
  if (IS_MAIN) serviceOutdoor(now);
}
