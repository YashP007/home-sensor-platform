# Power Architecture

The board has two DC barrel jacks (J1, J2), joined at the 5V rail through an SS34 Schottky diode. This document explains why.

## The problem

The base sensing platform draws under 100 mA continuous — WiFi transmit peaks at ~250 mA, the BME280 draws under 1 mA, and Qwiic accessories rarely exceed 20 mA each. A single 5V/1A supply is more than adequate for this workload, and a single-input design would have been simpler.

The complication is the Level 4 target: a WS2812B 22×22 matrix (484 LEDs) intended to display Spotify album art on the same board. This matrix draws up to 4A at 5V during high-brightness content, with fast pulsed current characteristics as the WS2812B's internal PWM cycles. Sharing a single 5V rail between a high-current pulsed load and analog sensor circuitry creates two problems:

1. **Voltage sag on the sensor rail during LED transients.** A 4A current pulse across the resistance of PCB traces, connector contact resistance, and the wall adapter's output impedance produces measurable dips on the 5V rail. The BME280 tolerates supply variation gracefully, but the XIAO's onboard 3.3V LDO propagates enough of the sag to affect ADC references, biasing the thermistor and light sensor readings.

2. **Ground bounce coupling into analog measurements.** The pulsed return current from the matrix shares a ground path with the sensor circuits. Even with a good ground pour, the shared return introduces measurable noise on analog channels — particularly problematic for the thermistor divider, whose output is a voltage referenced to the analog ground.

## The solution

Two independent 5V inputs, joined through a Schottky diode at the shared point. J1 (labeled logic input) feeds the XIAO and the Qwiic + sensor rail. J2 (labeled matrix input) feeds only the WS2812B connector. Ground is shared — this is unavoidable, since the WS2812B data line needs a common reference — but the current paths are isolated up to the ground plane.

The SS34 Schottky (D1) prevents backfeed between the two supplies. If the user powers only J1, the diode blocks J2's absence from pulling the shared node down. If the user powers only J2, the diode blocks J1's absence. When both are powered, whichever supplies higher voltage conducts, and the other is reverse-biased. The 550 mV forward drop of the SS34 at 3A is acceptable because the WS2812Bs operate correctly down to ~4.3V.

```
         J1 (5V logic input)
              │
              ▼
       ┌──── SS34 ────┐
       │              │
       │              ▼
       │        [XIAO ESP32-C3]
       │              │
       │              ▼
       │        [Qwiic + Sensors]
       │
       ▼
   [WS2812B via J5]
       ▲
       │
    J2 (5V matrix input)
```

## Practical operating modes

**Level 1–3 (sensing only):** Power J1 with a single 5V/1A wall adapter. J2 remains unconnected. Total system current is under 300 mA peak.

**Level 4 (matrix + sensing):** Power J1 with a 5V/1A adapter for the logic domain, and J2 with a separate 5V/5A adapter for the matrix. This preserves clean analog measurements while providing the current headroom for the LEDs. Total load is bounded by the matrix's software-capped brightness limit (`FastLED.setMaxPowerInVoltsAndMilliamps()`).

**Level 4 alternate (single supply):** Power J1 only with a 5V/5A adapter, leaving J2 unconnected. This works but analog sensor readings will be measurably noisier during high-brightness LED content. Acceptable for a display-primary use case where sensor accuracy is secondary.

## Bulk capacitance placement

The 100 µF electrolytic capacitor (C1) sits directly at the WS2812B connector (J5), not near the XIAO. This is deliberate: bulk capacitance is most effective when placed close to the load it is buffering. The LED matrix's transient current draw is what motivates the capacitor's existence, so it lives at the load end of the trace rather than the source end. A 10 µF ceramic (C2) provides high-frequency decoupling near the XIAO's power pins.

## Why not a switching regulator on-board

An earlier design revision considered accepting a 12V input and stepping it down to 5V with an on-board buck converter. This was rejected for three reasons:

1. **The BOM cost of a 5A-rated buck IC, its inductor, and output capacitors** (~$5) approximately doubles the total assembled cost of the board.
2. **The board area required for adequate copper pour and thermal management** on a 5A switching regulator increases total area by roughly 30%, which at $5/in² fab pricing is another $2–3 per board.
3. **The design responsibility shift from "commodity wall adapter" to "custom switching supply"** introduces EMI, layout, and thermal considerations that scale poorly with the number of boards being built. Off-loading power conversion to a UL-listed wall adapter is strictly cheaper, more reliable, and safer.

The dual-input barrel jack approach is the correct decomposition for a hobbyist-scale project. A commercial product might revisit this with a higher-input DC/DC converter to enable PoE, 12V automotive power, or similar; those are not requirements here.
