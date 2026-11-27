/*
 * SmartHome Monitor — Level 1: Basic Monitor
 *
 * Hardware bring-up check: reads temperature and humidity from a BME280 over
 * I2C and publishes both to Adafruit IO every 30 seconds. If this runs, the
 * board, sensor, WiFi and Adafruit IO account are all working.
 *
 * Hardware : XIAO ESP32-C3 on the SmartHome Monitor PCB, BME280 on J3 or J4 (Qwiic)
 * Libraries: Adafruit BME280, Adafruit Unified Sensor, Adafruit IO Arduino
 * Setup    : copy ../secrets_example.h to ./secrets.h and fill in WiFi + Adafruit IO.
 */

#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>
#include "AdafruitIO_WiFi.h"
#include "secrets.h"

// ─── Configuration ──────────────────────────────────────────────────────
// These are the living-room feeds that Level 2's main board also publishes
// to. Flash Level 1 on that board, or point these at scratch feeds.
#define FEED_TEMP  "home.home-temperature"
#define FEED_HUMID "home.home-humidity"

constexpr uint32_t PUBLISH_INTERVAL_MS   = 30000;
constexpr uint32_t IO_CONNECT_TIMEOUT_MS = 60000;

// ─── Globals ────────────────────────────────────────────────────────────
Adafruit_BME280 bme;
AdafruitIO_WiFi io(IO_USERNAME, IO_KEY, WIFI_SSID, WIFI_PASS);
AdafruitIO_Feed *feedTemp  = io.feed(FEED_TEMP);
AdafruitIO_Feed *feedHumid = io.feed(FEED_HUMID);

uint32_t lastPublishMs = 0;

// ─── Setup ──────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(2000);                                    // let the serial monitor attach
  Serial.println("\n[BOOT] SmartHome Monitor Level 1 — basic monitor");

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
}

// ─── Main loop ──────────────────────────────────────────────────────────
void loop() {
  io.run();                                       // MQTT keep-alive; call every iteration

  uint32_t now = millis();
  if (lastPublishMs != 0 && now - lastPublishMs < PUBLISH_INTERVAL_MS) return;
  lastPublishMs = now;

  float tempF    = bme.readTemperature() * 9.0f / 5.0f + 32.0f;
  float humidity = bme.readHumidity();
  Serial.printf("[LOOP] T=%.2fF  H=%.1f%%\n", tempF, humidity);

  bool ok = feedTemp->save(tempF);
  ok &= feedHumid->save(humidity);
  Serial.println(ok ? "[IO]   Published." : "[IO]   ERROR: publish failed.");
}
