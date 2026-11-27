# Firmware

Firmware for the SmartHome Monitor is organized as progressive tiers. Each tier is a self-contained Arduino sketch that compiles and runs on the same hardware.

| Tier | What it does |
|---|---|
| [Level 1 — Basic monitor](level1_basic_monitor/) | BME280 temperature + humidity to Adafruit IO. The hardware bring-up check |
| [Level 2 — Room sensor](level2_room_sensor/) | The deployed firmware. One sketch, two roles: a living-room **main** board (BME280 + TSL2591 light + outdoor temperature) and a bedroom **peripheral** board (BME280) |
| [Level 4 — Spotify album-art display](level4_future_spotify_display/) | Future work: design notes only |

Alongside the numbered tiers, `dev_*` directories hold work in progress toward the Spotify display. Each is a small, isolated proof of one piece of that pipeline, built and tested before the pieces are combined into a real sketch.

## Common setup

Every tier reads credentials from a `secrets.h` next to the sketch:

1. Copy `secrets_example.h` to `secrets.h` in the directory of the sketch you are compiling.
2. Fill in your WiFi SSID and password, and your Adafruit IO username and key. For Level 2, also set `DEVICE_ROLE` and the outdoor location.
3. `secrets.h` is gitignored. Never commit it.

Your Adafruit IO key grants full write access to your account. Treat it like a password. If you accidentally commit it, rotate the key from the Adafruit IO dashboard immediately.

## Required Arduino libraries

Install via the Arduino IDE Library Manager:

| Library | Purpose | Used by |
|---|---|---|
| `Adafruit BME280 Library` | BME280 driver | Level 1, 2 |
| `Adafruit Unified Sensor` | Sensor abstraction base | Level 1, 2 |
| `Adafruit IO Arduino` | MQTT client for Adafruit IO | Level 1, 2 |
| `Adafruit TSL2591 Library` | Ambient light sensor | Level 2 |
| `ArduinoJson` (v7) | Open-Meteo / Spotify JSON parsing | Level 2, dev_spotifyMCU |
| `FastLED` | WS2812B driver | Level 4 (future) |

Both Level 2 roles build from the same sketch, so install the full Level 2 set even for a peripheral-only board.

Board support: install "esp32" by Espressif Systems via the Boards Manager (Additional URLs: `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`). Select "XIAO_ESP32C3" as the target. Tested with esp32 core 3.3.12.

`dev_spotifyMCU` only needs ArduinoJson on top of the board package. `WiFiClientSecure`, `HTTPClient`, `Preferences` and `mbedtls/base64.h` all ship with it.

## Adafruit IO free tier

The free tier allows **10 feeds** and **30 messages per minute**, shared by every board on the account. Level 2 is designed around this limit: both boards together use 9 feeds and well under 30 messages/min. Diagnostics are packed into a single text feed per board instead of one feed each. See [Level 2 — Adafruit IO feeds](level2_room_sensor/README.md#adafruit-io-feeds).

Free-tier data is kept for 30 days. Export the feeds to CSV at least every few weeks, and analyse them with [`collected-data/`](../collected-data/).

## Spotify display stepping stones

The Spotify display is a lot to get right in one sketch: OAuth, TLS, JSON, JPEG decode and LED output all at once. These directories isolate one piece at a time and get it working on its own first.

### [dev_spotifyAPI](dev_spotifyAPI/)

Desktop Python harness for the Spotify Web API: OAuth 2.0 + PKCE, polling `currently-playing`, downloading album art. No hardware involved. The point is to work out the API contract somewhere with a debugger before porting request shapes to the ESP32.

### [dev_spotifyMCU](dev_spotifyMCU/)

The XIAO ESP32-C3 authenticates against Spotify (refresh-token flow, no client secret, no on-device browser step), polls `currently-playing`, and fetches the album art over HTTPS when the track changes. No JPEG decode or LED output yet. The art is dumped over serial and reconstructed on the desktop to confirm the round trip works before either of those gets built.

## Coding conventions

- Global constants in `ALL_CAPS_WITH_UNDERSCORES`. Feed keys are `#define`d at the top of each sketch, with the group prefix (`home.home-temperature`).
- `secrets.h` holds credentials and per-board settings only.
- Serial output is tagged with a bracketed subsystem identifier: `[BOOT]`, `[WIFI]`, `[IO]`, `[BME]`, `[TSL]`, `[WX]`, `[LOOP]`.
- No `delay()` in the main loop. All timing uses `millis()` comparisons.
- Every network call is guarded by a connectivity check and a timeout. A board that cannot reach its sensor or Adafruit IO restarts instead of halting.
- `io.run()` is called on every loop iteration.
