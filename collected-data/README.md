# collected-data - analysis

Turns the Adafruit IO exports into weekly dashboards, record-wide plots, summary
CSVs and a `summary.md` written to be handed (with the PNGs) to an AI model for
interpretation.

Two scripts share one package (`homesense/`), split by data era:

| Script | Covers | Feeds |
| --- | --- | --- |
| `analyze.py` | first light-feed sample → now | indoor temp, outdoor temp, lamp/sun lux (+ humidity / pressure if exported) |
| `analyze_legacy.py` | everything before that | indoor temp only; outdoor reference from Open-Meteo |

## Run it

```bash
cd collected-data
python analyze.py                    # multi-feed era
python analyze.py --forecast         # + tonight's pre-heat advice (Open-Meteo forecast)
python analyze.py --day 2026-10-21   # + one-day dashboard (repeatable)
python analyze_legacy.py             # temperature-only history
```

Other flags: `--since` / `--until`, `--raw-dir`, `--out`, `--no-weather`, `--no-html`.
Requirements: `pip install -r ../requirements.txt` (numpy, pandas, matplotlib, scipy, plotly).

## Input

Drop exports into `raw-data/` unchanged. Files are matched by name,
`home-<feed>-<YYYYMMDD>-<HHMM>.csv`, for these feeds:

| feed | column | grid fill |
| --- | --- | --- |
| `temperature` | `indoor_f` | interpolate gaps ≤ 6 min |
| `outdoor-temp` | `outdoor_f` | interpolate gaps ≤ 45 min; Open-Meteo hourly elsewhere |
| `led-lux` | `led_lux` | hold last value ≤ 10 min (published on change) |
| `sun-lux` | `sun_lux` | hold last value ≤ 10 min |
| `humidity`, `pressure` | `humidity_pct`, `pressure_hpa` | hold ≤ 70 min (optional) |
| `bedroom-temperature` | `bedroom_f` | interpolate gaps ≤ 6 min (Level 2 peripheral board) |
| `bedroom-humidity` | `bedroom_humidity_pct` | hold ≤ 70 min |
| `living-status`, `bedroom-status` | `data/device_status.csv` | text `key=value` diagnostics, parsed per message |

`indoor_f` is the living room (the original `home-temperature` feed). Lux values at the firmware's
0.01 lux floor are read back as 0.

Overlapping exports are de-duplicated by record id. Open-Meteo responses are
cached in `raw-data/_cache/`.

## Cleaning

- Out-of-range samples and spikes (> 3 °F from a 15-sample rolling median) are dropped.
- The 15 min after a power-up (first sample, or any gap > 2.5 min) are dropped. The
  board reads warm while settling, and one boot produced a 359 °F EMA.
- **Sun on the sensor:** while `sun_lux` > 2500 (padded 5 min before / 25 min after)
  the BME280 reads up to ~20 °F above room air. Those minutes are moved to
  `indoor_raw_f`, drawn dotted under a yellow band, and excluded from every statistic.

## Outputs

`analysis_output/<current|legacy>/` (wiped and rewritten each run):

```text
summary.md / summary.json    headline numbers, flags, thermal model, pre-heat guide, column dictionary
data/daily_summary.csv       one row per calendar day
data/weekly_summary.csv      one row per ISO week (Mon-Sun)
data/nights.csv              one row per night: lights-out/wake + overnight coast + model check
data/preheat_guide.csv       indoor °F needed at 18:00 / 22:00 to stay ≥ 65/68/70 °F at 06:00
data/hourly_timeseries.csv   hourly means of every feed, for ad-hoc analysis
data/device_status.csv       every status-feed message (uptime, RSSI, heap, raw TSL2591 counts)
data/data_health.csv, data/flags.csv
plots/weekly/week_<YYYY-Www>.png   temperature + light + activity dashboard per week
plots/calendar_heatmap.png   day × hour indoor and outdoor temperature
plots/day_hour_maps.png      day × time-of-day (rows 03:00→03:00): indoor, outdoor, LED lux, sun/IR lux
plots/indoor_outdoor_delta.png  indoor − outdoor gap by day × time of day + daily mean + median profile
plots/daily_trends.png       daily range/mean + hours below / in / above comfort
plots/spectrograms.png       per-feed spectrogram (period × time) + median spectrum
plots/thermal_model.png      model fit (coupling, solar gain, overnight validation)
plots/overnight_coast.png    nightly cooling curves + pre-heat guide
plots/nightly_activity.png   lights-out / lamp / daylight per night
explorer.html                zoomable plotly view (needs internet for the plotly CDN)
```

## Method

**Thermal model.** A single-time-constant passive model of the living room:

```text
dT/dt = K_COUPLE·(T_out − T) + K_SOLAR·log10(1 + sun_lux) + K_BIAS
```

It is fitted on passive samples (|dT/dt| ≤ 0.08 °F/min, 31-min slope window) with
a robust soft-L1 loss, and τ gets a day-block bootstrap CI. `summary.md` prints the
coefficients. τ = 1/K_COUPLE is the time
constant, and K_BIAS/K_COUPLE is how far above outdoor the apartment settles with
no heat and no sun.

**Overnight coast and pre-heat guide.** Each night from 22:00 to 06:00 is
measured. The model is replayed from 22:00 with the real outdoor trace, and its
error is reported in `pred_err_f`. The pre-heat guide inverts the model:
`T_start = T∞ + (target − T∞)·e^(L/τ)`.

**Activity.** The living-room "dark" threshold is learned from the night-time lux
floor. The dark run covering 03:00 is treated as the lights-out window, with lamp
blips under 10 min merged. Lamp use is only judged while the sun is down, because
the LED/sun split is uncalibrated and sunlight leaks into `led_lux` by day. The
sensor sits in the living room, so these times are a bedtime proxy.

**Rooms.** With the bedroom feeds present, the weekly dashboards and explorer draw both
rooms, daily/weekly tables add bedroom stats and `living_minus_bedroom_f`, and bedroom time outside
the comfort band is flagged per week.

**Devices and light calibration.** From the status feeds: restarts (uptime going backwards), WiFi
RSSI and free heap per board. The living-room board's raw counts give suggested `R_SUN` (median
ir/full with the sun > 20° up and ≥ 1000 lux) and `R_LED` (sun < −6°, ≥ 2 lux) for the Level 2 firmware.

**Comfort band** is 65-80 °F. Days are local calendar days (America/New_York).

## Tuning

Drop a `config.json` next to the scripts to override any key in
`homesense/config.py` (top-level keys replace the default wholesale), e.g.:

```json
{"comfort_low_f": 66, "morning_targets_f": [66, 68], "sensor_sunlit_lux": 3000}
```
