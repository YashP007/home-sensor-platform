# Design Rationale

This document explains the "why" behind the electrical and architectural decisions on the SmartHome Monitor board. Where a decision has meaningful tradeoffs, both sides are discussed rather than only the chosen path.

## MCU selection — Seeed XIAO ESP32-C3

The candidates were the Adafruit QT Py ESP32-S3, the Seeed XIAO ESP32-S3, and the Seeed XIAO ESP32-C3. All three offer 2.4 GHz WiFi, native USB-C, and a small castellated form factor suitable for surface-mount reflow onto a custom carrier.

**Chosen: XIAO ESP32-C3 (~$5.50).** For the sensing workload — a BME280 read every 5 seconds, a slope regression across a 12-sample buffer, and an MQTT publish to Adafruit IO — the C3's single-core RISC-V at 160 MHz operates at well under 5% CPU utilization. The 400 KB SRAM comfortably accommodates the WiFi + mbedTLS working set (~60 KB) with ample headroom for the application layer. WiFi is functionally equivalent to the S3 variants.

**Rejected: QT Py ESP32-S3 (~$13.50).** The dual-core Xtensa architecture and onboard STEMMA QT connector are genuine advantages for compute-heavy workloads, particularly the future Spotify album-art display where TLS, JPEG decoding, and LED framebuffer streaming compete for CPU time. For the current sensing workload, however, the ~$8 price premium buys no functional improvement. The design is scoped to be C3-compatible for the sensor tiers; the Spotify tier will be documented as requiring an S3 or better if performance testing during Level 4 development confirms the C3 cannot service TLS and JPEG decode simultaneously.

**Rejected: XIAO ESP32-S3 (~$7.50).** Would have been the ideal compromise, offering dual-core performance at a modest price premium. Excluded from the current design because the C3 was already in inventory from a prior project; a future revision may standardize on the S3 variant for pin-compatible upgradability.

The tradeoff to acknowledge: the C3 is single-core. If the Spotify tier proves infeasible on C3 during Level 4 development, the PCB will need a revision to swap the module footprint, since the XIAO C3 and S3 have pin-compatible castellations but different internal pin-to-GPIO mappings that affect firmware portability.

## Sensor selection — BME280 external, not on-board

The board hosts no on-board environmental sensor. The BME280 lives on a Qwiic breakout connected to J3 or J4. This is a deliberate choice, not a compromise.

**Why not on-board:** the BME280 (or SHT40, or any temperature sensor) reads accurately only when thermally isolated from heat sources. The XIAO ESP32-C3 dissipates ~150 mW during WiFi transmit, which raises the temperature of nearby PCB copper by several degrees Celsius. Placing a temperature sensor within 15 mm of the MCU introduces a systematic offset that varies with radio duty cycle — precisely the kind of error that is difficult to characterize and calibrate out. On-board placement with a thermal isolation slot (a peninsula connected by a narrow neck) mitigates this at the cost of board area and thermal response time.

**Why Qwiic-external is preferable here:** the Qwiic breakout with a 100 mm cable places the sensor physically away from the enclosure, in ambient air. This eliminates the MCU thermal coupling entirely and allows the sensor to sit in the airflow being measured, not the sealed enclosure microclimate. The cost is a 4-pin connector (~$0.55) and a Qwiic BME280 module (~$10) — a strictly better outcome than on-board integration for this application.

**Why BME280 over SHT40:** the BME280 adds barometric pressure sensing at the same price point, providing a useful correlate for weather-driven HVAC decisions and a channel that can be used to detect door/window opening events via short-term pressure transients. The SHT40 has marginally better humidity accuracy (±1.8% vs BME280's ±3%) but the BME280's pressure channel is a more valuable addition for indoor climate inference.

## Power topology — dual barrel jacks

The board has two DC barrel jacks (J1, J2), joined at the 5V rail through an SS34 Schottky diode. See [`power-architecture.md`](power-architecture.md) for the full discussion.

## Expansion strategy — Qwiic + THT breadboard pads + analog headers

Three parallel expansion pathways serve different phases of development:

**Qwiic connectors (J3, J4)** enable plug-and-play I2C sensor addition using SparkFun and Adafruit STEMMA QT ecosystem parts. No soldering required for expansion; supports daisy-chaining.

**THT I2C header (labeled "I2C" on silkscreen)** exposes SDA, SCL, 3V3, and GND at 0.1" pitch. Enables jumper-wire prototyping against a breadboard during firmware development for a new sensor before committing to a Qwiic breakout purchase.

**Analog sensor headers (SENS)** consist of a 2-pin header for a resistive sensor (feeds the 10kΩ divider into an ADC pin) and a 4-pin header for a phototransistor or three-terminal analog device. This handles the class of sensors that do not exist as Qwiic breakouts — legacy NTC thermistors, discrete photoresistors, current-sense shunts, and so on.

Together, these three pathways ensure that adding a new sensing modality does not require a PCB revision, regardless of whether the sensor is I2C-native or analog.

## What was deliberately excluded

Several features were considered and rejected. Documenting the exclusions is as important as documenting the inclusions.

**Reverse-polarity protection MOSFET.** A P-channel MOSFET across the input would have prevented damage from a reversed barrel jack. Rejected because barrel jacks have fixed polarity when paired with a specific wall adapter, the user (myself) is the only person likely to plug in the wrong supply, and the failure cost is a single $5 XIAO module. The 50 mV forward drop and $0.50 BOM cost were not justified.

**Battery charging circuit.** A MCP73831 single-cell LiPo charger with a JST-PH battery connector would have added ~50 mm² of board area and $1 in BOM. Rejected because the device is a permanent wall-mounted installation; portability is not a requirement, and the wall adapter is more reliable than any battery over multi-year operation.

**On-board level shifter for WS2812B data line.** A SN74LVC1T45 would translate the XIAO's 3.3V data output to 5V logic levels compliant with the WS2812B datasheet. Rejected because the ESP32's fast rise times reliably drive WS2812Bs at 3.3V over short cables in practice, despite technically violating the datasheet's 0.7 × VDD threshold. Empirical testing during Level 4 development will confirm; if unreliable, a level shifter will be added in v1.1.

**Real-time clock.** A DS3231 would preserve time across power loss. Rejected because the ESP32-C3's WiFi-native NTP client synchronizes time within seconds of boot at zero BOM cost. An RTC would be relevant only for offline operation, which is out of scope.

Each of these could be reconsidered in a future revision if requirements change. The rejections are documented so that a future me — or a future reader — understands they were considered and dismissed for specific reasons, not overlooked.
