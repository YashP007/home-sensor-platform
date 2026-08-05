# collected-data — temperature analysis

Analysis pipeline for the BME280 temperature feed exported from Adafruit IO.
Designed to be re-run unchanged on each new export (every 1–2 weeks).

## Run it

```bash
cd collected-data
python analyze_temperature.py                      # globs *.csv in raw-data/
python analyze_temperature.py export1.csv export2.csv
python analyze_temperature.py --weather            # add outdoor-temp cross-check
python analyze_temperature.py --no-plots
```

Drop new Adafruit IO exports into `raw-data/`; that's where auto-discovery looks.

Requires only `numpy`, `pandas`, `matplotlib`. No scipy, no API keys.

## Input

Adafruit IO feed export: `id,value,feed_id,created_at,lat,lon,ele`, with
`created_at` in **UTC** and `value` in °F. Multiple files are concatenated and
de-duplicated by record `id`, so overlapping exports are safe to drop in
together.

## What it does

| Step | Detail |
|---|---|
| Timezone | UTC → `America/New_York` (all analysis and reporting is local) |
| Day segmentation | 03:00 → 03:00 local. Partial days are analysed and flagged with `coverage_pct` |
| Resampling | Uniform 30 s grid; outages > 5 min are left as NaN so no slope is ever computed across a gap |
| AC event detection | Rolling least-squares dT/dt; a cooling run below −0.15 °F/min lasting ≥ 1.5 min and dropping ≥ 0.5 °F is an event. Boundaries refined to the compressor-start knee and the recovery trough |
| Transient rejection | An event is rejected only if it *both* follows a rise > +0.5 °F/min *and* fails to end ≥ 0.75 °F below its pre-event baseline. Rise rate alone cannot separate a disturbance from a genuine cycle, because real cycles also start off a fast recovery leg — the discriminator is whether the event left the room actually cooler. Rejected events are still reported with the numbers behind the call |
| Confidence flag | Events that pass the rejection test but still end < 0.75 °F below baseline are **counted and flagged `low`**. Short near-setpoint cycling late in the evening and a local disturbance look identical at this sampling rate; the ambiguity is surfaced rather than silently resolved |
| Period stats | Cooling and recovery-heating slope per event, aggregated over night / morning / afternoon / evening |
| Setpoint-raise coast | The longest AC-free warming stretch — the afternoon window where the thermostat sits near 86 °F |
| Thermal time constant | See below |

### Time-of-day periods

| Period | Window (local) |
|---|---|
| night | 22:00 – 05:00 |
| morning | 05:00 – 10:00 |
| afternoon | 10:00 – 18:00 |
| evening | 18:00 – 22:00 |

### Thermal time constant

Lumped first-order model, HVAC off: `dT/dt = (T∞ − T)/τ`.

Two independent estimators are reported:

1. **Curve fit (primary)** — least-squares fit of
   `T(t) = T∞ − (T∞ − T0)·e^(−t/τ)`. For a fixed τ the model is linear in
   `(T∞, T0)`, so it is solved in closed form on a log-spaced τ grid. Fitting
   the temperature curve is far better conditioned than fitting its derivative.
2. **Rate regression (cross-check)** — OLS of `dT/dt` on `T`; `τ = −1/slope`,
   `T∞ = −intercept/slope`.

Passive stretches are split into **monotone runs** before fitting, because a
stretch that warms and then cools is being driven by a moving outdoor
temperature and violates the single-asymptote assumption.

**Identifiability gate.** Over a window of length `L` a first-order response
closes `1 − e^(−L/τ)` of its gap. When that fraction is small the exponential is
indistinguishable from a straight line: τ runs off toward infinity and T∞
becomes meaningless *even though R² looks excellent*. Fits closing < 35% of the
gap are reported but excluded from the summary and marked NOT IDENTIFIABLE on
the plots.

## Outputs

Written to `analysis_output/`. Filenames are stamped with the **data's**
datetime range (not the run time), so re-running on the same export overwrites
in place and a new export lands in new files.

```
analysis_output/
  reports/
    hvac_analysis_<start>_to_<end>_daily_summary.csv    per-day totals, coverage, τ
    hvac_analysis_<start>_to_<end>_ac_events.csv        one row per detected cycle
    hvac_analysis_<start>_to_<end>_period_stats.csv     slope stats per day × period
    hvac_analysis_<start>_to_<end>_thermal_tau.csv      every fit, accepted or not
    hvac_analysis_<start>_to_<end>_setpoint_raise.csv   the afternoon coast
    hvac_analysis_<start>_to_<end>_report.txt           the terminal report
  plots/
    raw_full_record.png            whole record, events shaded, day boundaries
    days_overlaid.png              all analysis days on one 03:00→03:00 axis
    period_slope_summary.png       slope distributions + event counts by period
    day_<date>_diagnostic.png      per-day trace + dT/dt with every fit drawn
    day_<date>_cycle_detail.png    zoom on the deepest cycle — how slopes are measured
    day_<date>_thermal_tau.png     τ fits with residuals and identifiability
```

## Tuning

Drop a `config.json` next to the script to override any default without
editing code. Every key in `DEFAULT_CONFIG` is overridable, e.g.:

```json
{
  "timezone": "America/New_York",
  "cool_slope_on": 0.15,
  "artifact_rise_f_per_min": 0.50,
  "periods": {"night": [22, 5], "morning": [5, 10],
              "afternoon": [10, 18], "evening": [18, 22]}
}
```

The `--weather` flag pulls hourly outdoor temperature from Open-Meteo (no key
required) for a physically anchored τ. It fails soft: if the network is
unavailable the indoor-only estimate is reported alone.

## Related

`docs/HVAC_STATE_BUG.md` — why the `home-hvac-state` feed sat pinned at `1`,
and the threshold calibration this analysis feeds back into the firmware.
