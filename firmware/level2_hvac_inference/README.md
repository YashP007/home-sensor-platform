# Level 2 — HVAC State Inference

Detects whether the HVAC system (furnace in winter, AC in summer) is currently running, by measuring the temperature slope over a rolling 60-second window. Approximately 200 lines.

## The idea in one paragraph

An HVAC system does not directly report its state to a homeowner without professional integration. A room thermometer, however, indirectly reveals HVAC state: when the furnace turns on, the temperature rises; when it turns off, the temperature falls back toward equilibrium. If we sample temperature frequently enough and compute its rate of change, we can infer the HVAC state from the sign and magnitude of that rate — no smart thermostat required.

## Why this is harder than it sounds

Three practical problems complicate the naive "slope > 0 means heating on" approach:

**Sensor noise.** A single reading can jump ±0.1°F due to sensor noise even when the room temperature is truly stable. This gets classified as a positive or negative slope depending on which direction the noise pushes. Solution: exponential smoothing (EMA) with α = 0.25.

**Sensor resolution.** The original implementation used a DHT11 with 1°C (~1.8°F) resolution. This produces a staircase-shaped temperature trace even when the room is warming smoothly: several samples read the same integer value, then jump up by one. The apparent slope is zero during flat portions and infinite at the steps. Solution: a "dead zone" for slopes below ±0.20°F/min, during which the state machine's confirmation counters are held rather than reset. The BME280 has 0.01°C resolution, so this compensation matters much less, but it is retained because it also handles the naturally flat portions between HVAC cycles when the room reaches thermal equilibrium.

**False positives from transients.** A door opening for 10 seconds produces a brief negative slope in winter that a naive detector would misinterpret as HVAC turning off. Solution: require 3 consecutive slope confirmations (15 seconds at a 5-second sample rate) before declaring a state change.

## Algorithm at a glance

```
Every 5 seconds:
    1. read temperature from BME280
    2. apply EMA smoothing:  T_ema = 0.25 * T_new + 0.75 * T_ema
    3. push (timestamp, T_ema) into a circular buffer
    4. find the buffer sample >= 60 seconds old
    5. slope = (T_ema_now - T_old) / (t_now - t_old) in °F/min
    6. classify slope:
         - strongOn:   clear signal HVAC is running
         - strongOff:  clear signal HVAC is off
         - deadZone:   slope magnitude below 0.20 °F/min
    7. run state machine:
         - if currently OFF and strongOn: increment onConfirm
         - if 3 consecutive strongOn: declare HVAC ON, publish to Adafruit IO
         - (symmetric for ON → OFF transition)
    8. every 30 seconds, publish current temperature and state
```

## Season mode

The code has a single-line configuration at the top:

```cpp
constexpr uint8_t SEASON_MODE = 0;   // 0 = WINTER, 1 = SUMMER
```

In WINTER mode, a positive slope means the furnace is running. In SUMMER mode, a negative slope means the AC is running. Everything else in the algorithm is symmetric. Switching modes requires a recompile and re-upload.

A future improvement would be to move `SEASON_MODE` to a runtime configuration parameter fetched from Adafruit IO, so the mode can be changed from the dashboard without a firmware upload.

## Threshold calibration

The three threshold constants are:

| Constant | Value | Meaning |
|---|---|---|
| `SLOPE_ON_F_PER_MIN` | 0.25 | Slope magnitude above which we consider HVAC clearly ON |
| `SLOPE_OFF_F_PER_MIN` | 0.05 | Slope magnitude in the opposite direction that indicates OFF |
| `SLOPE_DEADZONE` | 0.20 | Slope magnitude below which the signal is treated as ambiguous |

These values were derived from analyzing several days of DHT11 data collected in a small apartment during heating cycles. The values are approximate and may need adjustment for larger rooms (slower thermal response, lower slope magnitudes) or high-power HVAC (faster response, higher slope magnitudes).

To recalibrate: leave Level 1 running for a few days with high-frequency publishing (every 10 seconds), export the data from Adafruit IO, plot temperature vs. time in Python, and observe the slope during known HVAC ON periods. Set `SLOPE_ON_F_PER_MIN` to approximately the 25th percentile of observed ON-slopes.

## Setup

Prerequisites are the same as Level 1. Additionally:

1. Create the feed `home-hvac-state` in the Adafruit IO dashboard.
2. Set `SEASON_MODE` at the top of the sketch to match your current season.
3. Optionally adjust the slope thresholds if your environment differs from a small apartment.

## Expected serial output

```
[BOOT] SmartHome Monitor Level 2 — HVAC State Inference
[BOOT] Season mode: WINTER
[BME]  Sensor initialized.
[WIFI] Connecting.....
[IO]   Adafruit IO Connected
[LOOP] T=68.42F  slope=+0.02F/min  hvac=OFF  confirm=0
[LOOP] T=68.51F  slope=+0.15F/min  hvac=OFF  confirm=0
[LOOP] T=68.79F  slope=+0.31F/min  hvac=OFF  confirm=1
[LOOP] T=69.12F  slope=+0.42F/min  hvac=OFF  confirm=2
[HVAC] HEAT ON @ 69.4F  slope=+0.48F/min  cycle #1
[LOOP] T=69.63F  slope=+0.51F/min  hvac=ON   confirm=0
...
[HVAC] HEAT OFF @ 72.1F  slope=-0.09F/min
```

## What this does not (yet) do

- No rolling 24-hour or 7-day statistics (was in the original implementation, deferred here for readability).
- No cycle-count aggregation over long periods.
- No adaptive threshold learning (the thresholds are compile-time constants).

The full original implementation with these features exists in project history but was simplified for the portfolio version. Level 3 reintroduces some of this complexity in a more general framework.
