"""Defaults, overridable by a config.json next to analyze.py (top-level keys only)."""

from __future__ import annotations

import copy
import json
import os

DEFAULTS = {
    # Locale
    "timezone": "America/New_York",
    "latitude": 42.35,                # Boston; used for sun position + Open-Meteo
    "longitude": -71.11,

    # Comfort band (°F), applied around the clock
    "comfort_low_f": 65.0,
    "comfort_high_f": 80.0,

    # Time-of-day periods, [start_hour, end_hour) local; wrapping midnight allowed
    "periods": {
        "night":   [22, 6],
        "morning": [6, 10],
        "day":     [10, 17],
        "evening": [17, 22],
    },

    # Cleaning
    "valid_range": {                  # samples outside these are dropped as sensor faults
        "indoor_f": [40, 100],
        "bedroom_f": [40, 100],
        "bedroom_humidity_pct": [0, 100],
        "outdoor_f": [-40, 120],
        "led_lux": [0, 120000],
        "sun_lux": [0, 120000],
        "humidity_pct": [0, 100],
        "pressure_hpa": [850, 1100],
    },
    "spike_window_samples": 15,       # centered rolling median for spike rejection
    "spike_threshold": {"indoor_f": 3.0, "bedroom_f": 3.0, "humidity_pct": 8.0, "bedroom_humidity_pct": 8.0},
    # Level 2 firmware publishes dark as 0.01 lux (log-scale charts cannot draw 0); read it back as 0.
    "lux_floor": 0.01,
    # After a power-up (first sample, or a gap longer than warmup_gap_min) the board is
    # still settling and reads warm; drop that stretch of indoor samples.
    "warmup_gap_min": 2.5,            # publish cadence is 30 s; a gap this long is usually a reboot
    "warmup_drop_min": 15,
    # Direct sun on the sensor heats it well above room air (seen: +10..+25 °F around 14:00).
    # Indoor readings while sun_lux exceeds this (padded before/after) are masked as
    # "sensor sunlit": kept in indoor_raw_f and the plots, excluded from every statistic.
    "sensor_sunlit_lux": 2500,
    "sensor_sunlit_pad_min": [5, 25],

    # Where the multi-feed era starts. None = first light-feed sample.
    # analyze.py runs from here on; analyze_legacy.py runs everything before it.
    "multi_feed_start": None,

    # Thermal model
    "slope_window_min": 31,           # centered OLS window for dT/dt (odd)
    "fit_step_min": 10,               # sub-sample the 1-min grid for the regression
    "fit_max_abs_slope": 0.08,        # °F/min; faster than this is HVAC/cooking/door, not drift
    "fit_min_samples": 60,
    "coast_start_hour": 22,           # overnight coast window, local
    "coast_end_hour": 6,
    "preheat_start_hours": [18, 22],  # pre-heat guide: indoor temp needed at these hours...
    "morning_targets_f": [65, 68, 70],  # ...to still be at/above these at coast_end_hour
    "preheat_outdoor_range_f": [20, 60, 5],

    # Activity (lux)
    "dark_floor_percentile": 5,       # of sun-down LED lux = the room's "lights off" floor
    "dark_floor_factor": 1.6,         # dark if lux <= max(floor*factor, floor+margin)
    "dark_floor_margin_lux": 0.5,
    "light_blip_merge_min": 10,       # lit blips shorter than this don't end a dark run
    "night_anchor_hour": 3,           # the dark run covering this hour is the sleep run
    "sun_down_elevation_deg": -3.0,   # civil-ish twilight cutoff for "daylight"
    "min_sleep_h": 2.0,

    # Spectrograms (plots/spectrograms.png)
    "spectrogram_window_h": 48,       # STFT window; 48 h resolves the daily cycle (0.5 cycles/day bins)
    "spectrogram_max_gap_frac": 0.2,  # windows missing more than this are left blank

    # Day x time-of-day maps (plots/day_hour_maps.png, plots/indoor_outdoor_delta.png)
    "dayhour_start_hour": 3,          # each row runs from this hour to the same hour next day
    "dayhour_bin_min": 15,            # column width

    # Light-split calibration from the living-room status feed (raw TSL2591 counts)
    "calib_sun_min_lux": 1000,        # strong daylight: sun dominates any lamp
    "calib_sun_min_elev_deg": 20,
    "calib_led_max_elev_deg": -6,     # after dusk every lux is artificial
    "calib_led_min_lux": 2,
    "calib_min_counts": 200,          # below this the ir/full ratio is noise (matches firmware)
    "calib_min_samples": 10,

    # Outdoor backfill (Open-Meteo, no key)
    "weather_backfill": True,
    "weather_timeout_s": 20,
}


def load_config(script_dir: str) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = os.path.join(script_dir, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cfg.update(json.load(fh))
            print(f"[cfg]  overrides loaded from {path}")
        except (OSError, ValueError) as exc:
            print(f"[cfg]  WARNING: could not parse {path}: {exc}; using defaults")
    return cfg


def period_of_hour(hour: float, periods: dict) -> str:
    for name, (a, b) in periods.items():
        if (a <= b and a <= hour < b) or (a > b and (hour >= a or hour < b)):
            return name
    return "unassigned"
