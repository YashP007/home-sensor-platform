# Level 2b — Context-aware HVAC inference

Level 2 slope-regression furnace/AC detection plus TSL2591 light (LED vs sun split)
and Open-Meteo outdoor temperature. Level 2 stays untouched as the baseline.

## Libraries
Adafruit BME280, Adafruit TSL2591, Adafruit Unified Sensor, Adafruit IO Arduino,
ArduinoJson v7. Board: XIAO_ESP32C3. TSL2591 on Qwiic (J3/J4), address 0x29.

## Adafruit IO feeds (group `home`)
Create these before flashing (publishes to missing feeds fail silently).

| Feed key | Unit | Cadence | Purpose |
|---|---|---|---|
| home.home-temperature | °F | 30 s | existing, indoor |
| home.home-humidity / home-pressure | %RH / hPa | on change + 1 h | existing |
| home.home-hvac-state | text | edge + 5 min | existing, `HEAT-ON`/`HEAT-OFF` |
| home.home-outdoor-temp | °F | 15 min | Open-Meteo |
| home.home-led-lux | lux | ≥5 % change, ≥30 s, 5 min heartbeat | artificial light |
| home.home-sun-lux | lux | same | sunlight |
| home.home-heat-on | 0/1 | edge + 5 min | analysis (numeric, plottable) |
| home.home-heat-on-base | 0/1 | edge + 5 min | only when `COMP_ENABLED` (A/B) |

Optional overrides in `secrets.h`: `IO_FEED_OUTDOOR`, `IO_FEED_LED_LUX`,
`IO_FEED_SUN_LUX`, `IO_FEED_HEAT_ON`, `IO_FEED_HEAT_ON_BASE`, `OUTDOOR_LAT`, `OUTDOOR_LON`.

## Calibrate before trusting the split
`R_SUN` / `R_LED` are placeholders. Set `LIGHT_CALIBRATION 1`, read the `ir/full`
ratio at midday with lamps off (R_SUN) and at night on LEDs only (R_LED), in the
final enclosure. Set back to 0.

## Rollout
1. `COMP_ENABLED=false`, `K_*=0`: log for 1–2 weeks.
2. Fit `dT/dt = K_COUPLE*(T_out-T) + K_SOLAR*log10(1+sun_lux) + K_BIAS` on heat-OFF segments.
3. Paste coefficients, set `COMP_ENABLED=true`, re-derive `DET_COMP_*` on the residual.

Slope thresholds were calibrated on summer AC data; furnace rates are unmeasured.
