# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — 2026-11 — Multi-room firmware

### Firmware
- **Level 2 — Room sensor** (`level2_room_sensor/`) replaces Level 2b. One sketch, two roles chosen by `DEVICE_ROLE` in `secrets.h`: a living-room main board (BME280, TSL2591 sun/LED light split, Open-Meteo outdoor temperature) and a bedroom peripheral board (BME280).
- Fits the Adafruit IO free tier across both boards: change-triggered numeric feeds plus one packed `key=value` status text feed per board (uptime, RSSI, free heap, raw TSL2591 counts).
- Lux is published with a 0.01 floor so Adafruit IO log-scale charts no longer break on dark readings.
- Level 1 publishes temperature and humidity only, keeps feed keys in the sketch, and restarts instead of halting on sensor or connection faults.
- Spotify display tier renumbered from Level 4 to Level 3.

### Removed
- HVAC state inference (old Level 2 and the Level 2b detectors / drift compensation). It never produced a usable signal on this hardware.
- Level 3 VEML7700 + thermistor tier, which targeted sensors the deployed boards do not have.
- Pressure publishing.

### Analysis
- `collected-data/` homesense package: multi-feed dashboards, thermal model, overnight coast / pre-heat guide, light-based activity.
- Multi-room support (bedroom feeds), status-feed parsing, board health, and suggested `R_SUN` / `R_LED`.

## [v1.0] — 2026-07 — Initial PCB release

### Hardware
- First-revision PCB (`HomeTemp_PCB_v13`) designed around the Seeed XIAO ESP32-C3.
- Dual barrel-jack power inputs (5V logic, 5V matrix) with Schottky diode isolation on the shared 5V rail (SS34).
- Two SparkFun Qwiic connectors (JST-SH) for I2C sensor chaining.
- One 3-pin JST-SM connector for a WS2812B LED matrix data + power interface.
- Resistive divider circuit (10kΩ fixed) for NTC thermistor or future analog resistive sensors.
- 4-pin sensor header for a phototransistor or discrete analog input.
- 100 µF bulk capacitance at the LED matrix connector to buffer pulsed current draw.

### Firmware
- **Level 1** — BME280 environmental data published to Adafruit IO at a fixed interval.
- **Level 2** — HVAC state inference using rolling temperature slope regression with dead-zone hysteresis (removed in Unreleased).
- **Level 3** — Multi-modal expansion with VEML7700 ambient light, thermistor divider read, and I2C bus scan (removed in Unreleased).

### Documentation
- Design rationale for MCU, sensor, and power topology choices.
- Assembly guide covering solder order and validation checkpoints.
- Enclosure design with FDM-oriented dovetail wall mount.

### Known limitations
- LED matrix data line is driven at 3.3V from the XIAO GPIO. WS2812B nominal VIH at 5V VDD is 3.5V, which is technically out of spec but reliable in this configuration over the short JST cable. If chaining multiple matrices, add a SN74LVC1T45 level shifter on the data line.
- No hardware watchdog beyond the ESP32-C3's internal WDT. Firmware relies on the SDK's default 3-second task WDT.

## [Planned] — v1.1

### Hardware
- Migrate from EAGLE to KiCad 8 for the schematic and layout.
- Add a footprint for a discrete SN74LVC1T45 level shifter (populate-optional) on the LED matrix data line.
- Move the SS34 diode footprint to allow easier probe access.
- Add silkscreen indicators for barrel jack polarity.

### Firmware
- **Level 3** — Spotify album-art display via WS2812B matrix. Requires OAuth 2.0 flow, TLS to `api.spotify.com`, JPEG decode, and 64→22 downsample.
- Store the light-split calibration (`R_SUN` / `R_LED`) in NVS instead of hardcoding it.
