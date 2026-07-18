# Firmware

Firmware for the SmartHome Monitor is organized as four progressive tiers. Each tier is a self-contained Arduino sketch that compiles and runs independently on the same hardware. Higher tiers add capability without breaking the interface established by lower tiers.

## Common setup

All firmware tiers share a common credentials file. Before compiling any tier:

1. Copy `secrets_example.h` to `secrets.h` in the same directory as the sketch you are compiling.
2. Populate `secrets.h` with your WiFi SSID, WiFi password, Adafruit IO username, and Adafruit IO key.
3. Verify `secrets.h` appears in `.gitignore` — it should never be committed.

Your Adafruit IO key grants full write access to your account. Treat it like a password. If you accidentally commit it, rotate the key immediately from the Adafruit IO dashboard.

## Required Arduino libraries

Install via the Arduino IDE Library Manager:

| Library | Purpose | Used by tiers |
|---|---|---|
| `Adafruit BME280 Library` | BME280 driver | 1, 2, 3 |
| `Adafruit Unified Sensor` | Sensor abstraction base | 1, 2, 3 |
| `Adafruit IO Arduino` | MQTT client for Adafruit IO | 1, 2, 3 |
| `Adafruit VEML7700 Library` | Ambient light sensor | 3 |
| `FastLED` | WS2812B driver | 4 (future) |
| `ArduinoJson` | JSON parser for Spotify API | 4 (future) |

Board support: install "esp32" by Espressif Systems via the Boards Manager (Additional URLs: `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`). Select "XIAO_ESP32C3" as the target.

## Tier overview

### [Level 1 — Basic monitor](level1_basic_monitor/)
Approximately 80 lines. BME280 read every 5 seconds, published to Adafruit IO. This is the "does the hardware work" checkpoint. If Level 1 does not run, no higher tier will run.

### [Level 2 — HVAC state inference](level2_hvac_inference/)
Approximately 200 lines. Adds a rolling temperature buffer, slope regression, and dead-zone hysteresis to detect whether HVAC is heating, cooling, or idle. Publishes both raw sensor data and the inferred state to separate Adafruit IO feeds.

### [Level 3 — Multi-modal expandable platform](level3_expandable_platform/)
Approximately 350 lines. Adds VEML7700 ambient light reading, thermistor divider ADC, and an I2C bus scan at startup that logs any Qwiic devices found. Structured with a per-sensor abstraction so that adding a new I2C sensor requires modifying one function.

### [Level 4 — Spotify album-art display](level4_future_spotify_display/) (future work)
Roadmap only — no firmware yet. Describes the OAuth 2.0 flow, TLS requirements, and JPEG decode pipeline that will drive the WS2812B matrix.

## Coding conventions

- Global constants in `ALL_CAPS_WITH_UNDERSCORES`.
- Serial output is tagged with a bracketed subsystem identifier: `[WIFI]`, `[IO]`, `[BME]`, `[HVAC]`, `[LOOP]`.
- No `delay()` in the main loop. All timing uses `millis()` comparisons.
- Every network call is guarded by a connectivity check and a timeout.
- The Adafruit IO `io.run()` call is invoked on every loop iteration.
