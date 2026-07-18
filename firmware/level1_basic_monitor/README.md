# Level 1 — Basic Environmental Monitor

The minimum viable version of this project. Reads a BME280 environmental sensor every 30 seconds and publishes to Adafruit IO. Approximately 80 lines of code.

## What this demonstrates

- I2C sensor initialization with graceful fallback across the two common BME280 addresses.
- Non-blocking main loop using `millis()`-based scheduling.
- WiFi + MQTT connection with a keep-alive loop.
- Structured Serial output for debugging.

## What this deliberately does not do

- No sensor state inference (that is Level 2).
- No multi-sensor support (that is Level 3).
- No LED matrix (that is Level 4).
- No sleep modes or power management.
- No OTA updates.

Level 1 is scoped to be the "first working version." Every added capability is deferred to a later tier so that Level 1 remains readable at a glance.

## Setup

1. Copy `../secrets_example.h` to `secrets.h` in this directory. Populate all fields.
2. Confirm the following feeds exist in your Adafruit IO dashboard:
   - `home-temperature`
   - `home-humidity`
   - `home-pressure`
   If any feed name is missing, publishes to that feed will silently fail.
3. Connect a BME280 breakout to J3 or J4 via a Qwiic cable.
4. Open `level1_basic_monitor.ino` in Arduino IDE. Board: `XIAO_ESP32C3`. Upload.

## Expected serial output

```
[BOOT] SmartHome Monitor Level 1 — Basic Environmental Monitor
[BME]  Sensor initialized.
[WIFI] Connecting to Adafruit IO....
[IO]   Connected: Adafruit IO Connected
[LOOP] T=23.42C (74.16F)  H=42.1%  P=1013.2hPa
[IO]   Published successfully.
[LOOP] T=23.44C (74.19F)  H=42.0%  P=1013.2hPa
[IO]   Published successfully.
```

The first publish appears approximately 30 seconds after boot (`PUBLISH_INTERVAL_MS`), not immediately. This is intentional — a lower rate would leave more headroom for the Adafruit IO free-tier data rate cap (30 messages per minute across all feeds).

## Troubleshooting

**Sensor not found at either 0x76 or 0x77.** The Qwiic cable is not seated, the BME280 breakout is not receiving power, or the pull-up resistors are missing. Verify with `i2cscan` example sketch from the ESP32 core.

**WiFi never connects.** The XIAO ESP32-C3 supports only 2.4 GHz. Confirm your SSID broadcasts on 2.4 GHz, not 5 GHz only.

**Publishes reported successful but no data in the Adafruit IO dashboard.** Feed names in `secrets.h` do not exactly match the feeds in your dashboard. Names are case-sensitive.
