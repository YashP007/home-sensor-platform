/*
 * SmartHome Monitor — Level 3: Multi-Modal Expandable Platform
 *
 * Extends Level 2 with:
 *   - VEML7700 ambient light sensor (via Qwiic).
 *   - Thermistor divider on ADC (via the 2-pin SENS header).
 *   - I2C bus scan at boot, logging every device found.
 *   - Per-sensor abstraction so that adding a new I2C sensor requires
 *     modifying only the SensorRegistry.
 *
 * Design goal:
 *   Demonstrate a scaling pattern rather than a monolithic sketch.
 *   Each sensor has an initialization function, a read function, and a
 *   publish function. Adding a new sensor is a matter of adding one entry
 *   to the registry.
 *
 * Published feeds (in addition to Level 2):
 *   - home-lux: ambient light in lux from VEML7700
 */

#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>
#include <Adafruit_VEML7700.h>
#include "AdafruitIO_WiFi.h"
#include "secrets.h"

// ─── Configuration ──────────────────────────────────────────────────────

constexpr uint32_t PUBLISH_INTERVAL_MS = 30000;
constexpr uint8_t  THERMISTOR_PIN      = A1;      // GPIO3 — see hardware/README.md
constexpr float    THERMISTOR_NOMINAL_R = 10000.0f;
constexpr float    THERMISTOR_NOMINAL_T = 25.0f;
constexpr float    THERMISTOR_BETA      = 3950.0f;
constexpr float    DIVIDER_FIXED_R      = 10000.0f;
constexpr float    ADC_REF_V            = 3.30f;
constexpr uint16_t ADC_MAX              = 4095;   // ESP32-C3 default 12-bit ADC

// ─── Sensor abstraction ─────────────────────────────────────────────────

struct Sensor {
  const char *name;                     // human-readable identifier
  bool        available;                // populated by begin()
  bool      (*begin)();                 // initialization returns success
  float     (*read)();                  // returns current value or NAN on failure
  AdafruitIO_Feed *feed;                // Adafruit IO destination (nullable)
  const char *unit;                     // for serial output only
};

// ─── Global objects ─────────────────────────────────────────────────────

Adafruit_BME280   bme;
Adafruit_VEML7700 veml;
AdafruitIO_WiFi   io(IO_USERNAME, IO_KEY, WIFI_SSID, WIFI_PASS);

AdafruitIO_Feed *feedTemp  = io.feed(IO_FEED_TEMP);
AdafruitIO_Feed *feedHumid = io.feed(IO_FEED_HUMID);
AdafruitIO_Feed *feedPress = io.feed(IO_FEED_PRESS);
AdafruitIO_Feed *feedLux   = io.feed(IO_FEED_LUX);

uint32_t lastPublishMs = 0;

// ─── Sensor implementations ─────────────────────────────────────────────

bool  bme_begin()      { return bme.begin(0x77, &Wire) || bme.begin(0x76, &Wire); }
float bme_read_temp()  { return bme.readTemperature() * 9.0f / 5.0f + 32.0f; }
float bme_read_humid() { return bme.readHumidity(); }
float bme_read_press() { return bme.readPressure() / 100.0f; }

bool  veml_begin()     { return veml.begin(); }
float veml_read()      { return veml.readLux(); }

bool  therm_begin()    { pinMode(THERMISTOR_PIN, INPUT); return true; }
float therm_read() {
  // Steinhart-Hart simplified (β equation) for a 10kΩ NTC in a 10kΩ divider.
  uint16_t raw = analogRead(THERMISTOR_PIN);
  if (raw == 0 || raw >= ADC_MAX) return NAN;    // open or shorted
  float v_out    = (raw * ADC_REF_V) / ADC_MAX;
  float r_therm  = DIVIDER_FIXED_R * (ADC_REF_V / v_out - 1.0f);
  float steinhart = log(r_therm / THERMISTOR_NOMINAL_R) / THERMISTOR_BETA;
  steinhart += 1.0f / (THERMISTOR_NOMINAL_T + 273.15f);
  float tempC = 1.0f / steinhart - 273.15f;
  return tempC * 9.0f / 5.0f + 32.0f;
}

// ─── Sensor registry ────────────────────────────────────────────────────
// To add a new sensor: add one entry here, and (if I2C) create a new feed
// name in secrets.h. Nothing else in the sketch needs to change.

Sensor sensors[] = {
  { "BME280 temperature", false, bme_begin,   bme_read_temp,  feedTemp,  "F"    },
  { "BME280 humidity",    false, nullptr,     bme_read_humid, feedHumid, "%"    },  // reuses bme_begin
  { "BME280 pressure",    false, nullptr,     bme_read_press, feedPress, "hPa"  },  // reuses bme_begin
  { "VEML7700 light",     false, veml_begin,  veml_read,      feedLux,   "lux"  },
  { "Thermistor (SENS)",  false, therm_begin, therm_read,     nullptr,   "F"    },  // no feed for now
};
constexpr size_t SENSOR_COUNT = sizeof(sensors) / sizeof(sensors[0]);

// ─── I2C bus scanner (for observability at boot) ────────────────────────

void scanI2C() {
  Serial.println("[I2C]  Scanning bus for connected devices...");
  uint8_t found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("[I2C]  Device found at 0x%02X\n", addr);
      found++;
    }
  }
  if (found == 0) Serial.println("[I2C]  No devices found. Check Qwiic cabling.");
  else            Serial.printf("[I2C]  Total: %u device(s).\n", found);
}

// ─── Setup ──────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(2000);
  Serial.println();
  Serial.println("[BOOT] SmartHome Monitor Level 3 — Multi-Modal Platform");

  Wire.begin();
  analogReadResolution(12);
  scanI2C();

  // Initialize each sensor. Sensors that share a begin() function (e.g. all
  // BME280 channels) are covered by the first entry's begin().
  bool bmeReady = false;
  for (size_t i = 0; i < SENSOR_COUNT; i++) {
    if (sensors[i].begin) {
      sensors[i].available = sensors[i].begin();
      Serial.printf("[SENS] %-24s : %s\n", sensors[i].name,
                    sensors[i].available ? "OK" : "not found");
      if (i == 0) bmeReady = sensors[i].available;
    } else {
      // Inherits availability from the previous begin() (BME280 sub-channels).
      sensors[i].available = bmeReady;
    }
  }

  Serial.print("[WIFI] Connecting");
  io.connect();
  while (io.status() < AIO_CONNECTED) { Serial.print("."); delay(500); }
  Serial.printf("\n[IO]   %s\n", io.statusText());
}

// ─── Main loop ──────────────────────────────────────────────────────────

void loop() {
  io.run();

  uint32_t now = millis();
  if (now - lastPublishMs < PUBLISH_INTERVAL_MS) return;
  lastPublishMs = now;

  Serial.printf("[LOOP] t=%lus\n", now / 1000);
  for (size_t i = 0; i < SENSOR_COUNT; i++) {
    if (!sensors[i].available) continue;
    float v = sensors[i].read();
    if (isnan(v)) {
      Serial.printf("[SENS] %-24s : (read failed)\n", sensors[i].name);
      continue;
    }
    Serial.printf("[SENS] %-24s : %8.2f %s\n", sensors[i].name, v, sensors[i].unit);
    if (sensors[i].feed) {
      if (!sensors[i].feed->save(v)) {
        Serial.printf("[IO]   ERROR: publish failed for %s\n", sensors[i].name);
      }
    }
  }
}
