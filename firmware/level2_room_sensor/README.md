# Level 2 — Room Sensor (main + peripheral)

One sketch runs on every board in the house. A single line in `secrets.h` chooses the board's role:

| Role | Room | Sensors | Publishes |
|---|---|---|---|
| `ROLE_MAIN` | living room | BME280, TSL2591 (Qwiic, 0x29) | temperature, humidity, lamp/LED lux, sun lux, outdoor temperature, status |
| `ROLE_PERIPHERAL` | bedroom | BME280 | temperature, humidity, status |

Each board connects to Adafruit IO on its own. Neither board depends on the other, so either can be unplugged without affecting the other.

## What this demonstrates

- **Compile-time roles.** `DEVICE_ROLE` picks the room name, the feed keys and the hardware. Code for the other role is compiled out: the peripheral build drops the TLS/JSON weather client and is ~50 KB smaller.
- **Working inside the Adafruit IO free tier** (10 feeds, 30 messages/min, shared by both boards). Values worth charting get their own numeric feed and are only published when they change. Diagnostics are packed into one text feed per board (see [Status feed](#status-feed)).
- **Light source separation.** The TSL2591 reports a full-spectrum channel and an IR channel. Sunlight is IR-rich and LEDs are not, so the IR fraction splits total lux into a sun part and a lamp part. Outdoor shortwave radiation from Open-Meteo forces the sun part to zero at night.
- **Log-domain filtering** for lux, which spans 0.01 to 100 000. Dark is published as 0.01 lux, never 0, so Adafruit IO's log-scale charts draw it.

## Adafruit IO feeds

Create a group `home` and these feeds before flashing. Publishes to a feed that does not exist fail silently.

| Feed key | Board | Type | Cadence |
|---|---|---|---|
| `home-temperature` | main | °F | every 30 s |
| `home-humidity` | main | %RH | change ≥ 1 %RH (≥ 30 s apart), 30 min heartbeat |
| `home-led-lux` | main | lux | change ≥ 12 % (≥ 30 s apart), 5 min heartbeat |
| `home-sun-lux` | main | lux | same as `home-led-lux` |
| `home-outdoor-temp` | main | °F | every 15 min (Open-Meteo) |
| `home-living-status` | main | text | every 5 min, and at boot |
| `home-bedroom-temperature` | peripheral | °F | every 30 s |
| `home-bedroom-humidity` | peripheral | %RH | same as `home-humidity` |
| `home-bedroom-status` | peripheral | text | every 5 min, and at boot |

That is 9 feeds, leaving one free on the free tier. The main board keeps the original Level 1 feed keys, so the living-room history continues unbroken.

**Message budget.** Normal use is about 3–4 messages/min from the main board and about 2.5/min from the peripheral. In the worst case, with every change-triggered feed at its 30 s minimum gap, the totals are about 8.3 and 4.2/min. Both stay well under the shared limit of 30/min.

## Status feed

Each board publishes one text value every 5 minutes, as space-separated `key=value` pairs:

```text
main:        up=86400 rssi=-61 heap=182344 full=1534 ir=212 gain=25 lux=31.200
peripheral:  up=86400 rssi=-67 heap=201880
```

| Key | Meaning |
|---|---|
| `up` | seconds since boot. A drop means the board restarted |
| `rssi` | WiFi signal, dBm |
| `heap` | free heap, bytes. Steady decline = leak |
| `full`, `ir` | raw TSL2591 counts, full-spectrum and IR channels (main only) |
| `gain` | TSL2591 gain multiplier in use: 1, 25, 428 or 9876 (main only) |
| `lux` | total lux before the sun/LED split (main only) |

Adafruit IO cannot chart text, but the CSV export keeps it intact. `collected-data/analyze.py` parses it into `data/device_status.csv`, counts reboots, and estimates `R_SUN` / `R_LED` from the raw counts.

## Setup

1. Install the libraries listed in [`../README.md`](../README.md#required-arduino-libraries).
2. Copy `../secrets_example.h` to `secrets.h` in this directory and fill in WiFi and Adafruit IO credentials.
3. Set `DEVICE_ROLE` in `secrets.h`:
   - `ROLE_MAIN` for the living-room board. Also set `OUTDOOR_LAT` / `OUTDOOR_LON`; two decimals (≈ 1 km) is plenty for a weather-model grid.
   - `ROLE_PERIPHERAL` for the bedroom board.
4. Open `level2_room_sensor.ino`, select board `XIAO_ESP32C3`, and upload.
5. To flash the other board, change `DEVICE_ROLE` and upload again.

The TSL2591 is optional on the main board. If it is missing, the board logs a warning and keeps publishing temperature, humidity and outdoor temperature.

## Calibrating the light split

`R_SUN` and `R_LED` at the top of the sketch are the IR/full ratios under pure sunlight and pure LED light. The values in the sketch are placeholders. Until they are calibrated, some daytime sunlight is counted as lamp light.

1. Run the main board, inside its final enclosure, for a few days that include sunny middays and evenings with the lamps on. Any lid or diffuser changes the spectral response.
2. Export `home-living-status` from Adafruit IO into `collected-data/raw-data/` and run `python analyze.py`.
3. `summary.md` → *Devices* reports the median IR/full ratio in strong daylight (suggested `R_SUN`) and with the lamps on after dark (suggested `R_LED`). Paste them into the sketch and re-flash.

## Expected serial output

Illustrative values, main board:

```text
[BOOT] SmartHome Monitor Level 2 — room sensor: living (main)
[BME]  Sensor initialized.
[TSL]  Sensor initialized.
[WIFI] Connecting....
[IO]   Adafruit IO connected.
[LOOP] living T=70.84F H=37.9% | sun=0.01 led=41.87 lux
[IO]   humidity -> 37.93
[IO]   sun lux -> 0.01
[IO]   led lux -> 41.87
[IO]   status -> up=9 rssi=-58 heap=183412 full=1871 ir=96 gain=25 lux=41.870
[WX]   outdoor 38.4F  shortwave 0 W/m2
[IO]   outdoor -> 38.4
```

The peripheral prints the same `[LOOP]` line without the light and outdoor fields.

## Signal processing notes

- Temperature and humidity are sampled every 5 s and smoothed with an EMA (α = 0.25, ≈ 20 s time constant). Temperature is published every 30 s.
- The BME280 runs with pressure oversampling disabled, since pressure is not used.
- TSL2591 auto-gain steps through 1× / 25× / 428× / 9876×, re-reading when a sample saturates (≥ 30 000 counts) or is too dim (< 100). Below 200 counts the IR ratio is noise, so dim light is all counted as lamp light.
- The outdoor fetch backs off 1 → 2 → 4 → 8 → 15 min on failure. Outdoor data older than 90 min is not used to gate the sun channel.
- A missing BME280 or a failed Adafruit IO connection (60 s) restarts the board rather than halting, so an unattended board recovers on its own.
