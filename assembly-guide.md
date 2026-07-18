# Assembly Guide

This document walks through hand-assembly of a bare PCB. Estimated time for a first build is 45–90 minutes, dominated by solder inspection and cleanup rather than the soldering itself.

## Required tools

- Temperature-controlled soldering iron with a fine tip (0.4–0.8 mm chisel)
- Leaded solder, 0.5–0.7 mm diameter — leaded is easier to work with by hand than lead-free, at the cost of standard lead handling precautions
- Flux paste or flux pen
- Fine tweezers
- Isopropyl alcohol (99%) and a stiff brush for flux cleanup
- Digital multimeter with continuity mode
- 10× magnifier or USB microscope (strongly recommended, not optional for the QFN and 0603 components)

## Solder order

Follow this order. It is not arbitrary — earlier components are the ones that would be blocked or made harder to place if later components were installed first.

### 1. Passives (0603 resistors and capacitors)

Start with R1 (10K), R2 (330), R3 (70), R4/R5 (22Ω each), R6/R7 (2.4K each), and C2 (10 µF). Use the "flood and drag" technique: tin one pad on each footprint, place the component with tweezers while reflowing the tinned pad, then solder the opposite pad.

Passives are placed first because they sit flush against the board and any adjacent taller components would obstruct iron access. The 2.4 kΩ pull-ups (R6, R7) are on the I2C bus — verify orientation is not critical for resistors, but placement is.

### 2. SS34 Schottky diode (D1)

Two-terminal SMA package. Orient with the cathode band matching the silkscreen mark on the PCB. Reversed orientation will block the primary power path and the board will not power on from J1.

### 3. XIAO ESP32-C3 module (U2)

The module sits on the bottom side of the PCB, with its castellated edges facing down. Two mounting approaches:

**Reflow approach:** Apply a small drop of solder paste to each of the 14 castellated pads on the PCB. Place the module aligned to the silkscreen outline. Reflow with a hot air station at 280°C for 30–45 seconds, or use a hotplate.

**Iron approach:** Tin one corner pad. Place the module and reflow the tinned pad to hold position. Verify alignment (a millimeter of skew will still work electrically but looks unprofessional). Solder the diagonally opposite corner. Then work around the module soldering each remaining pad. Add flux liberally.

The XIAO's own USB-C connector should overhang the PCB edge — verify this before soldering, because it is the connection point for programming.

### 4. Barrel jacks (J1, J2)

Through-hole. Insert from the top side and solder from the bottom. These pads carry the primary current path and should be soldered generously — a poor connection here will drop voltage under load.

### 5. Qwiic connectors (J3, J4)

SMD JST-SH, 4-pin, 1.0 mm pitch. Tin one pad, place the connector, reflow to hold, then solder the remaining three pads and the two mechanical retention tabs. The retention tabs are structural — they carry insertion force, not signal. Neglecting them will result in the connector eventually pulling free.

### 6. WS2812B matrix connector (J5)

Through-hole 3-pin JST-SM header. Solder from the bottom side. Verify orientation matches the silkscreen — the pinout is (looking at the connector from the front) GND, DIN, 5V. Reversed polarity will destroy the first LED in the matrix chain.

### 7. Sensor and I2C debug headers

Through-hole 0.1" pin headers for the SENS and I2C breakouts, and the 2-pin thermistor header. Solder from the bottom side. These headers are optional — populate only what you need.

### 8. Bulk capacitor (C1)

100 µF electrolytic, radial through-hole. **Orientation is critical** — the longer lead is positive, and the negative side is marked by a stripe on the capacitor body. The PCB has a "+" silkscreen mark. Reversed polarity will result in the capacitor venting under load; at 5V this is more of a smoke event than a bang, but still to be avoided.

## Validation before power-on

Before applying power for the first time, verify:

1. **No solder bridges.** Inspect under magnification, particularly around the XIAO module's castellations and the Qwiic connectors' fine pitch.
2. **Continuity between GND pads.** All ground pads should be at the same node.
3. **No continuity between 5V and GND.** A short here means a solder bridge or a reversed capacitor. Trace and fix before applying power.
4. **No continuity between 3V3 and GND.** Same as above.
5. **Correct orientation of D1 and C1.** Verify against the silkscreen markings.

## First power-on

Apply 5V to J1 (only J1 for now — leave J2 unconnected). Immediately check:

- The XIAO's power LED illuminates.
- The 3V3 rail measures 3.30 ± 0.05 V at the I2C header.
- No components are audibly buzzing or getting warm.

If the XIAO's power LED does not illuminate, the most likely causes are (in order of frequency): SS34 orientation reversed, cold solder joint on one of the barrel jack pins, or a solder bridge on the XIAO's power pins.

Then connect a USB-C cable to the XIAO's onboard connector and confirm the module enumerates on your computer as `/dev/ttyACMx` (Linux/Mac) or a COM port (Windows). If it does not enumerate, the module itself is likely damaged from earlier heat exposure — try a different module.

## First firmware upload

Load `firmware/level1_basic_monitor/level1_basic_monitor.ino` in Arduino IDE. Board: "XIAO_ESP32C3" (install via Boards Manager if not present). Port: whatever the XIAO enumerated as. Upload.

Open the Serial Monitor at 115200 baud. Reset the XIAO. Expected output:

```
[BOOT] SmartHome Monitor Level 1 — Basic Environmental Monitor
[WIFI] Connecting to <SSID>...
[WIFI] Connected. IP address: 192.168.x.x
[IO]   Connecting to Adafruit IO...
[IO]   Connected.
[BME]  Sensor found at address 0x77.
[LOOP] T=23.4C  H=42.1%  P=1013.2hPa
[LOOP] Published to feed home_temp_F: 74.12
```

If the sensor is not found, verify the Qwiic cable is fully seated and try the alternate I2C address (0x76). Some BME280 breakouts strap to 0x76 by default, others to 0x77.

## Assembly-time issues and their causes

The most common assembly errors and their symptoms, roughly in order of how often I have hit them personally:

**Symptom: XIAO enumerates on USB but no 3V3 rail present at I2C header.** Cause: cold joint on the XIAO's 3V3 pad, or the 3V3 trace is broken between the module and the header. Reflow the XIAO's 3V3 pad and verify continuity with a multimeter.

**Symptom: Sensor not detected on I2C.** Cause: usually a bad Qwiic cable or an unpopulated pull-up resistor. Verify R6 and R7 are populated and their values are 2.4 kΩ. If populating both an on-board pull-up and a Qwiic breakout with its own pull-ups, the parallel combination may be too aggressive — remove R6/R7 or the breakout's pull-ups, but not both.

**Symptom: Board resets every few seconds when WiFi transmits.** Cause: insufficient supply current. The XIAO peaks at ~250 mA during WiFi transmit; a supply rated below 500 mA can droop enough to trigger the ESP32's brownout detector. Use a 1A supply, or add a larger bulk capacitor on the 5V rail.

**Symptom: BME280 reads 2–4°C higher than a reference thermometer.** Cause: the sensor is thermally coupled to a heat source, most commonly the XIAO's WiFi radio if the Qwiic cable is short and the sensor sits inside the enclosure. Extend the cable to place the sensor outside the enclosure in ambient air, or add ventilation slots to the enclosure.
