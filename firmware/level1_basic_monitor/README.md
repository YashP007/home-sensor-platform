# Level 1 — Basic Monitor

The hardware bring-up check. It reads temperature and humidity from a BME280 every 30 seconds and publishes both to Adafruit IO, in about 80 lines. If Level 1 runs, the board, sensor, WiFi and Adafruit IO account all work, and Level 2 will too.

## What this demonstrates

- I2C sensor initialization with fallback across the two common BME280 addresses (0x77, 0x76).
- A non-blocking main loop using `millis()`-based scheduling.
- WiFi + MQTT connection to Adafruit IO, with a restart if it cannot connect within 60 s.
- Tagged serial output for debugging.

Everything else (light sensing, outdoor temperature, multiple rooms) is in [Level 2](../level2_room_sensor/).

## Setup

1. Copy `../secrets_example.h` to `secrets.h` in this directory and fill in WiFi and Adafruit IO credentials. `DEVICE_ROLE` and the location are ignored by Level 1.
2. In Adafruit IO, create a group `home` with the feeds `home-temperature` and `home-humidity`. Publishes to a feed that does not exist fail silently.
   These are the living-room feeds that Level 2's main board uses. To test a second board without writing into that history, change `FEED_TEMP` / `FEED_HUMID` at the top of the sketch.
3. Connect a BME280 breakout to J3 or J4 with a Qwiic cable.
4. Open `level1_basic_monitor.ino` in the Arduino IDE, select board `XIAO_ESP32C3`, and upload.

## Expected serial output

```text
[BOOT] SmartHome Monitor Level 1 — basic monitor
[BME]  Sensor initialized.
[WIFI] Connecting....
[IO]   Adafruit IO connected.
[LOOP] T=71.42F  H=38.1%
[IO]   Published.
```

Readings repeat every 30 seconds. Two messages per 30 s is well inside the Adafruit IO free-tier limit of 30 messages per minute.

## Troubleshooting

**Sensor not found at 0x77 or 0x76.** The Qwiic cable is not seated, or the breakout has no power. Run the `i2cscan` example from the ESP32 core to see what is on the bus. The sketch restarts every 10 s until the sensor appears.

**WiFi never connects.** The XIAO ESP32-C3 only supports 2.4 GHz. Check that the SSID broadcasts on 2.4 GHz.

**Publishes succeed but no data appears.** The feed keys in the sketch do not match the dashboard. Keys are case-sensitive and include the group prefix (`home.`).
