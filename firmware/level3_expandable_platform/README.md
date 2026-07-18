# Level 3 — Multi-Modal Expandable Platform

Demonstrates the pattern for adding new sensors to the platform without restructuring the sketch. Approximately 200 lines. Includes VEML7700 ambient light, thermistor divider, and a runtime I2C bus scan.

## What this demonstrates

The value of Level 3 is not the specific sensors it reads, but the *pattern* for adding them. A common failure mode in Arduino projects is a monolithic sketch where each new sensor requires modifying multiple functions (setup, loop, publish, error handling). This scales poorly — every addition risks breaking existing functionality.

Level 3 uses a small abstraction: a `Sensor` struct with function pointers for `begin()` and `read()`, plus a static registry of `Sensor` instances. The main loop iterates the registry, calls each sensor's read function, and publishes if a feed is attached. Adding a new sensor is a matter of:

1. Writing a `begin()` and `read()` function that return `bool` and `float` respectively.
2. Adding one entry to the `sensors[]` array.
3. Optionally adding a new feed name in `secrets.h`.

Nothing in `setup()` or `loop()` needs to change.

## The Sensor abstraction

```cpp
struct Sensor {
  const char *name;
  bool        available;
  bool      (*begin)();
  float     (*read)();
  AdafruitIO_Feed *feed;
  const char *unit;
};
```

The `feed` pointer is nullable — a sensor that reads locally (for use in logic elsewhere) can be part of the registry without publishing.

## Sensors included

**BME280** — same as Level 1 and 2. Three channels (temperature, humidity, pressure) share a single `begin()` call, since one BME280 chip provides all three.

**VEML7700 ambient light** — connect a SparkFun Qwiic VEML7700 breakout to J3 or J4 alongside the BME280. Reads illuminance in lux, useful for correlating HVAC behavior with sunlight-driven passive heating.

**Thermistor divider** — a 10kΩ NTC thermistor connected to the 2-pin SENS header, forming a voltage divider with the on-board 10kΩ fixed resistor. Reads a second temperature channel using the ADC, useful as a cross-check against the BME280. Uses the β equation (simplified Steinhart-Hart) for the conversion from resistance to temperature.

The thermistor read function includes bounds checking on the ADC value: a reading of 0 or 4095 indicates an open circuit (thermistor unplugged) or short, and the function returns `NAN` in those cases rather than a garbage temperature.

## I2C bus scan at boot

The sketch scans all 127 possible I2C addresses at boot and logs every device that acknowledges. This is invaluable for debugging Qwiic chain issues: if a sensor does not appear in the scan, the problem is the physical connection or the sensor itself, not the driver code.

Example output:

```
[I2C]  Scanning bus for connected devices...
[I2C]  Device found at 0x10
[I2C]  Device found at 0x77
[I2C]  Total: 2 device(s).
```

0x10 is the VEML7700, 0x77 is the BME280. If the scan finds them, the driver initialization is guaranteed to be about firmware, not wiring.

## Adding a new sensor

Concrete example: adding a SparkFun Qwiic CO₂ sensor (SCD41 breakout, I2C address 0x62).

1. Install the SparkFun SCD4x library.
2. Add these to the sketch:

   ```cpp
   #include "SparkFun_SCD4x_Arduino_Library.h"
   SCD4x scd;
   bool  scd_begin() { return scd.begin(); }
   float scd_read()  { return scd.getCO2(); }
   ```

3. Add one line to the sensors registry:

   ```cpp
   { "SCD41 CO2", false, scd_begin, scd_read, feedCo2, "ppm" },
   ```

4. Add `#define IO_FEED_CO2 "home-co2"` to `secrets.h` and declare `feedCo2` at the top of the sketch.

That is the entire integration. The main loop will pick it up automatically. If the SCD41 is not connected, `scd_begin()` returns false, `available` is set to false, and the loop skips it silently.

## Setup

Prerequisites are the same as Levels 1 and 2, plus:

1. Install the `Adafruit VEML7700 Library` via Library Manager.
2. Create the feed `home-lux` in the Adafruit IO dashboard.
3. Optionally, connect a 10kΩ NTC thermistor to the 2-pin SENS header. If absent, the thermistor sensor entry will report read failures every publish cycle, which is cosmetic and does not affect other sensors.

## Expected serial output

```
[BOOT] SmartHome Monitor Level 3 — Multi-Modal Platform
[I2C]  Scanning bus for connected devices...
[I2C]  Device found at 0x10
[I2C]  Device found at 0x77
[I2C]  Total: 2 device(s).
[SENS] BME280 temperature       : OK
[SENS] VEML7700 light           : OK
[SENS] Thermistor (SENS)        : OK
[WIFI] Connecting....
[IO]   Adafruit IO Connected
[LOOP] t=30s
[SENS] BME280 temperature       :    74.13 F
[SENS] BME280 humidity          :    42.10 %
[SENS] BME280 pressure          :  1013.24 hPa
[SENS] VEML7700 light           :   328.42 lux
[SENS] Thermistor (SENS)        :    74.31 F
```

## Limitations of this pattern

The function-pointer registry approach is straightforward but has two shortcomings worth noting:

**Every sensor is polled at the same interval.** For sensors with different natural update rates (e.g., CO₂ at 30 seconds, IR gesture at 100 Hz), each would need its own interval field and last-publish timestamp. A production system would use a proper scheduler.

**Error handling is per-read, not stateful.** A sensor that fails intermittently will retry every publish cycle indefinitely. A production system would implement exponential backoff on repeated failures and periodic re-attempts to recover from transient issues.

Both are addressed in the roadmap for v1.1 firmware but are outside the scope of a portfolio example whose priority is clarity over completeness.
