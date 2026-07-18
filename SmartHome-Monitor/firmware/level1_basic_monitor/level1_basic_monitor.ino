/*
 * SmartHome Monitor — Level 1: Basic Environmental Monitor
 *
 * Reads temperature, humidity, and pressure from a BME280 sensor over I2C,
 * publishes to Adafruit IO every 30 seconds.
 *
 * Hardware:
 *   - SmartHome Monitor PCB v13 (or any XIAO ESP32-C3 with a Qwiic BME280)
 *   - BME280 breakout connected to J3 or J4 via a Qwiic cable
 *
 * Setup:
 *   1. Copy `secrets_example.h` (from firmware/ root) to `secrets.h` in
 *      this directory. Populate with your credentials.
 *   2. Create the following feeds in the Adafruit IO dashboard:
 *        - home-temperature
 *        - home-humidity
 *        - home-pressure
 *   3. Install the required libraries listed in firmware/README.md.
 *   4. Board: XIAO_ESP32C3. Upload.
 */

#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>
#include "AdafruitIO_WiFi.h"
#include "secrets.h"

// ─── Configuration ──────────────────────────────────────────────────────

constexpr uint8_t   BME280_I2C_ADDR    = 0x77;   // Adafruit default; try 0x76 if not found
constexpr uint32_t  PUBLISH_INTERVAL_MS = 30000;  // 30 seconds between publishes
constexpr float     SEALEVEL_PRESSURE_HPA = 1013.25f;

// ─── Globals ────────────────────────────────────────────────────────────

Adafruit_BME280 bme;
AdafruitIO_WiFi io(IO_USERNAME, IO_KEY, WIFI_SSID, WIFI_PASS);

AdafruitIO_Feed *feedTemp  = io.feed(IO_FEED_TEMP);
AdafruitIO_Feed *feedHumid = io.feed(IO_FEED_HUMID);
AdafruitIO_Feed *feedPress = io.feed(IO_FEED_PRESS);

uint32_t lastPublishMs = 0;

// ─── Setup ──────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(2000);  // Allow serial monitor to attach before first print

  Serial.println();
  Serial.println("[BOOT] SmartHome Monitor Level 1 — Basic Environmental Monitor");

  Wire.begin();

  if (!bme.begin(BME280_I2C_ADDR, &Wire)) {
    Serial.println("[BME]  ERROR: sensor not found at 0x77. Trying 0x76...");
    if (!bme.begin(0x76, &Wire)) {
      Serial.println("[BME]  ERROR: sensor not found at 0x76 either. Halting.");
      while (1) { delay(1000); }
    }
  }
  Serial.println("[BME]  Sensor initialized.");

  // Indoor navigation preset: 16x pressure oversampling, 2x temp, 1x humidity, 16x IIR filter.
  // Gives the most stable readings for a fixed indoor installation.
  bme.setSampling(Adafruit_BME280::MODE_NORMAL,
                  Adafruit_BME280::SAMPLING_X2,   // temperature
                  Adafruit_BME280::SAMPLING_X16,  // pressure
                  Adafruit_BME280::SAMPLING_X1,   // humidity
                  Adafruit_BME280::FILTER_X16,
                  Adafruit_BME280::STANDBY_MS_500);

  Serial.print("[WIFI] Connecting to Adafruit IO");
  io.connect();
  while (io.status() < AIO_CONNECTED) {
    Serial.print(".");
    delay(500);
  }
  Serial.println();
  Serial.print("[IO]   Connected: ");
  Serial.println(io.statusText());
}

// ─── Main loop ──────────────────────────────────────────────────────────

void loop() {
  // Adafruit IO housekeeping — must be called every loop iteration.
  io.run();

  if (millis() - lastPublishMs < PUBLISH_INTERVAL_MS) return;
  lastPublishMs = millis();

  // Read all three channels. Adafruit's driver blocks briefly on each call.
  float tempC     = bme.readTemperature();
  float tempF     = tempC * 9.0f / 5.0f + 32.0f;
  float humidity  = bme.readHumidity();
  float pressure  = bme.readPressure() / 100.0f;  // Pa → hPa

  Serial.printf("[LOOP] T=%.2fC (%.2fF)  H=%.1f%%  P=%.1fhPa\n",
                tempC, tempF, humidity, pressure);

  // Publish to Adafruit IO. save() is non-blocking and queues the message.
  if (feedTemp->save(tempF)   &&
      feedHumid->save(humidity) &&
      feedPress->save(pressure)) {
    Serial.println("[IO]   Published successfully.");
  } else {
    Serial.println("[IO]   ERROR: publish failed. Check network connectivity.");
  }
}
